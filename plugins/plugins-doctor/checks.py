"""Pure checks behind the ``plugins-doctor`` CLI.

Every check takes already-resolved paths and already-parsed mappings and returns frozen
:class:`Finding` rows. The only I/O is *reading* the plugin directories handed to it, so the
caller owns ``$HERMES_HOME`` resolution and nothing here mutates disk.

A verdict is a row, never a silent pass: a check that cannot run (missing host seam, unreadable
directory, corrupt state record) still reports its own row with ``warn`` or ``fail`` severity.

Logging uses the stdlib logger that the host (``hermes_cli.plugins``) and every sibling plugin
use: the host owns plugin logging and ``structlog`` is not a host dependency, so importing it
here would either add a forbidden dependency or push lines to stdout and corrupt the CLI table.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final, Literal, Protocol

logger = logging.getLogger(__name__)

Severity = Literal["ok", "warn", "fail"]
CheckId = Literal["links", "settings", "seams", "stale_import"]

SEVERITY_ORDER: Final[Mapping[Severity, int]] = {"ok": 0, "warn": 1, "fail": 2}

# Same acceptance as ``hermes_cli.plugins_discovery.scan_directory``: YAML manifests first, then a
# portable ``plugin.json`` package.
MANIFEST_FILENAMES: Final[tuple[str, ...]] = ("plugin.yaml", "plugin.yml", "plugin.json")
SOURCE_PATTERN: Final[str] = "*.py"

# README.md:51-57, the prose list of host contracts this repository's plugins build on.
REQUIRED_HOOKS: Final[tuple[str, ...]] = (
    "post_tool_call_settled",
    "post_tool_directive",
    "pre_verify",
    "post_compaction",
)
REQUIRED_CONTEXT_MEMBERS: Final[tuple[str, ...]] = (
    "cancel_model_call",
    "set_turn_reasoning",
    "register_role_resolver",
    "register_context_engine",
)
REQUIRED_MCP_KWARG: Final[str] = "replay_on_failure"
REQUIRED_MODULES: Final[tuple[str, ...]] = ("agent.verification_evidence", "tools.clarify_tool")

# Persisted load records (``ctx.state`` key ``loads``): newest last, one row per live pid.
LOAD_RECORDS_KEY: Final[str] = "loads"
RECORD_VERSION: Final[int] = 1
MAX_ARGV_CHARS: Final[int] = 256
DIGEST_PREVIEW_CHARS: Final[int] = 12
_PROC_ROOT: Final[Path] = Path("/proc")


@dataclass(frozen=True, slots=True)
class Finding:
    """One verdict row: what was checked, for which plugin, how bad, and why."""

    check: CheckId
    plugin: str | None
    severity: Severity
    detail: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "check": self.check,
            "plugin": self.plugin,
            "severity": self.severity,
            "detail": self.detail,
        }


class ManifestLike(Protocol):
    """The slice of ``hermes_cli.plugins_manifest.PluginManifest`` these checks read."""

    name: str
    config_schema: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PluginEntry:
    """One plugin directory entry: the name it is installed under and its resolved directory."""

    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class EntryScan:
    """Resolved entries of one plugins directory plus the findings that stopped others."""

    entries: tuple[PluginEntry, ...]
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class EnabledPlugin:
    """An enabled ``plugins.entries.<name>`` row, parsed once at the config boundary."""

    name: str
    settings: Mapping[str, object]
    settings_error: str | None


@dataclass(frozen=True, slots=True)
class LoadRecord:
    """One process's recorded plugin digests, parsed once at the state boundary."""

    pid: int
    argv0: str
    started_at: str
    plugins: tuple[tuple[str, str], ...]


def has_failure(findings: Sequence[Finding]) -> bool:
    """True when any row is a ``fail`` (the CLI's exit code)."""
    return any(finding.severity == "fail" for finding in findings)


def ordered_findings(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    """Failures first, then warnings, then passes; stable within a severity."""
    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                -SEVERITY_ORDER[finding.severity],
                finding.check,
                finding.plugin or "",
                finding.detail,
            ),
        )
    )


def plugin_dirs(home: Path, profile: str | None) -> tuple[Path, ...]:
    """User plugins dir, plus the profile-scoped one when *profile* names a non-default profile.

    Mirrors the host layout: ``plugins_state.get_config`` reads ``get_hermes_home()`` and the
    discovery sweep scans ``get_hermes_home() / "plugins"``, while ``hermes_cli.profiles`` places
    a named profile at ``<home>/profiles/<name>``.
    """
    primary = home / "plugins"
    name = (profile or "").strip()
    if not name or name == "default":
        return (primary,)
    return (primary, home / "profiles" / name / "plugins")


def scan_entries(plugins_dir: Path) -> EntryScan:
    """Resolve every entry of one plugins directory to a dir containing a manifest.

    Mirrors the host's discovery: ``<root>/<name>/`` carries a manifest, or is a category whose
    children ``<root>/<cat>/<name>/`` do (entry name ``cat/name``, one level deep). A dangling
    symlink, a symlink to a non-directory, or a directory with no manifest anywhere the host
    would look is a ``fail``: each one is a plugin the operator believes is installed and the
    host cannot load. A stray regular file is only a ``warn`` — it was never a plugin.
    """
    findings: list[Finding] = []
    entries: list[PluginEntry] = []
    try:
        children = sorted(plugins_dir.iterdir())
    except FileNotFoundError:
        return EntryScan((), (Finding("links", None, "fail", f"plugins directory does not exist: {plugins_dir}"),))
    except OSError as exc:
        return EntryScan((), (Finding("links", None, "fail", f"cannot list {plugins_dir}: {exc}"),))
    for child in children:
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        target, blocked = _resolve_entry(child)
        if target is None:
            if blocked is not None:
                findings.append(blocked)
            continue
        if _manifest_in(target) is not None:
            entries.append(PluginEntry(child.name, target))
            continue
        members = _category_members(child.name, target)
        if not members:
            findings.append(
                Finding("links", child.name, "fail", f"no manifest in resolved directory {target} or one level below it")
            )
            continue
        entries.extend(members)
    return EntryScan(tuple(entries), tuple(findings))


def _manifest_in(directory: Path) -> Path | None:
    """The manifest file the host would read from *directory*, or None."""
    return next((directory / name for name in MANIFEST_FILENAMES if (directory / name).is_file()), None)


def _category_members(category: str, directory: Path) -> tuple[PluginEntry, ...]:
    """Manifest-bearing children of a category dir, named ``category/child`` like the host key."""
    try:
        children = sorted(directory.iterdir())
    except OSError:
        return ()
    members: list[PluginEntry] = []
    for child in children:
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        target, _blocked = _resolve_entry(child)
        if target is not None and _manifest_in(target) is not None:
            members.append(PluginEntry(f"{category}/{child.name}", target))
    return tuple(members)


def _resolve_entry(child: Path) -> tuple[Path | None, Finding | None]:
    """Resolve one plugins-directory entry to a directory, or return the row explaining why not."""

    def blocked(severity: Severity, detail: str) -> tuple[None, Finding]:
        return None, Finding("links", child.name, severity, detail)

    if child.is_symlink():
        try:
            target = child.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return blocked("fail", f"dangling symlink -> {_symlink_target(child)} ({exc.__class__.__name__})")
        if not target.is_dir():
            return blocked("fail", f"symlink does not resolve to a directory -> {target}")
        return target, None
    if not child.is_dir():
        return blocked("warn", "not a directory; not a plugin entry")
    return child.resolve(), None


def _symlink_target(child: Path) -> str:
    """The recorded symlink target, or ``"?"`` when the filesystem refuses to say."""
    try:
        return os.readlink(child)
    except OSError:
        return "?"


def merged_entries(directories: Sequence[Path]) -> tuple[PluginEntry, ...]:
    """Union of the entries of *directories*; the first directory wins a name collision."""
    merged: dict[str, PluginEntry] = {}
    for directory in directories:
        for entry in scan_entries(directory).entries:
            merged.setdefault(entry.name, entry)
    return tuple(merged[name] for name in sorted(merged))


def check_links(plugins_dir: Path, profile_plugins_dir: Path | None = None) -> tuple[Finding, ...]:
    """(a) Every installed entry is a directory or symlink resolving to a dir with a manifest.

    The primary plugins dir must exist (a profile with no plugins dir loads nothing); an absent
    profile plugins dir is simply not a profile, so it produces no rows.
    """
    findings: list[Finding] = []
    seen: set[Path] = set()
    for directory, required in ((plugins_dir, True), (profile_plugins_dir, False)):
        if directory is None:
            continue
        marker = _resolved_or_self(directory)
        if marker in seen:
            continue
        seen.add(marker)
        if not required and not directory.is_dir():
            continue
        scan = scan_entries(directory)
        findings.extend(scan.findings)
        if any(finding.plugin is None for finding in scan.findings):
            continue  # the directory itself could not be read; no pass to report beside the fault
        findings.append(
            Finding("links", None, "ok", f"{directory}: {len(scan.entries)} plugin(s) resolve to a manifest")
        )
    return tuple(findings)


def _resolved_or_self(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def enabled_plugins(config: Mapping[str, object]) -> tuple[EnabledPlugin, ...]:
    """``plugins.entries`` rows the host would load, parsed once at the config boundary."""
    plugins = _as_mapping(config.get("plugins"))
    if plugins is None:
        return ()
    entries = _as_mapping(plugins.get("entries")) or {}
    listed = plugins.get("enabled")
    listed_names = {name for name in listed if isinstance(name, str)} if isinstance(listed, list) else set()
    names = set(listed_names)
    for name, entry in entries.items():
        parsed = _as_mapping(entry)
        if parsed is not None and parsed.get("enabled") is True:
            names.add(name)
    return tuple(_enabled_plugin(name, _as_mapping(entries.get(name)) or {}) for name in sorted(names))


def _enabled_plugin(name: str, entry: Mapping[str, object]) -> EnabledPlugin:
    settings = entry.get("settings")
    if settings is None:
        settings = entry.get("config")  # legacy migration subtree, same precedence as get_config
    if settings is None:
        return EnabledPlugin(name, {}, None)
    parsed = _as_mapping(settings)
    if parsed is None:
        return EnabledPlugin(name, {}, f"plugins.entries.{name}.settings must be a mapping")
    return EnabledPlugin(name, parsed, None)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def check_settings(
    config: Mapping[str, object], manifests: Mapping[str, ManifestLike]
) -> tuple[Finding, ...]:
    """(b) Every enabled plugin has a manifest and settings matching its ``config_schema``.

    Unknown keys are a ``warn`` (the host ignores them), a present value of the wrong type is a
    ``fail`` (the plugin's own reader will get a value it cannot honour), and a plugin the
    operator enabled with no discoverable manifest is a ``fail``.
    """
    validator = _host_validator()
    if validator is None:
        return (
            Finding(
                "settings",
                None,
                "fail",
                "host hermes_cli.plugins_manifest.validate_config_schema is unavailable",
            ),
        )
    findings: list[Finding] = []
    for plugin in enabled_plugins(config):
        findings.extend(_settings_findings(plugin, manifests.get(plugin.name), validator))
    return tuple(findings) or (Finding("settings", None, "ok", "no enabled plugins"),)


def _settings_findings(
    plugin: EnabledPlugin,
    manifest: ManifestLike | None,
    validator: Callable[[str, Mapping[str, object], Mapping[str, object]], list[str]],
) -> list[Finding]:
    if manifest is None:
        return [Finding("settings", plugin.name, "fail", "enabled but no plugin manifest was found")]
    if plugin.settings_error is not None:
        return [Finding("settings", plugin.name, "fail", plugin.settings_error)]
    schema = _as_mapping(manifest.config_schema) or {}
    findings = [
        Finding("settings", plugin.name, "warn", f"unknown setting {key!r} is not declared in config_schema")
        for key in sorted(set(plugin.settings) - set(schema))
    ]
    messages = validator(plugin.name, schema, plugin.settings)
    findings.extend(
        Finding("settings", plugin.name, _settings_severity(message, plugin.name, plugin.settings), message)
        for message in messages
    )
    if not findings:
        findings.append(Finding("settings", plugin.name, "ok", "settings match config_schema"))
    return findings


def _settings_severity(message: str, plugin_id: str, settings: Mapping[str, object]) -> Severity:
    """A present-but-wrong value fails; a missing optional/required key only warns.

    The host's messages are fixed at ``plugins.entries.<id>.settings.<key> should be ...`` and
    ``... is required ...``; a key that *is* set therefore failed the type check.
    """
    prefix = f"plugins.entries.{plugin_id}.settings."
    if message.startswith(prefix):
        key = message[len(prefix) :].split(" ", 1)[0]
        if key in settings:
            return "fail"
    return "warn"


def _host_validator() -> Callable[[str, Mapping[str, object], Mapping[str, object]], list[str]] | None:
    """The host's settings↔schema validator, or ``None`` when the host does not ship it."""
    try:
        from hermes_cli.plugins_manifest import validate_config_schema
    except ImportError:
        return None
    return validate_config_schema


def check_seams() -> tuple[Finding, ...]:
    """(c) The host seams README.md:51-57 names are all present on this host."""
    module = _host_plugins_module()
    if module is None:
        return (Finding("seams", None, "fail", "hermes_cli.plugins is not importable"),)
    findings: list[Finding] = []
    context_cls = getattr(module, "PluginContext", None)
    if context_cls is None:
        findings.append(Finding("seams", None, "fail", "PluginContext is missing from hermes_cli.plugins"))
    else:
        findings.extend(_context_seam_findings(context_cls))
    findings.append(_hooks_seam_finding(getattr(module, "VALID_HOOKS", None)))
    findings.extend(_module_seam_finding(name) for name in REQUIRED_MODULES)
    return tuple(findings)


def _context_seam_findings(context_cls: type) -> list[Finding]:
    findings = [
        Finding(
            "seams",
            None,
            "ok" if callable(getattr(context_cls, member, None)) else "fail",
            f"PluginContext.{member}",
        )
        for member in REQUIRED_CONTEXT_MEMBERS
    ]
    findings.append(_mcp_seam_finding(getattr(context_cls, "call_mcp", None)))
    return findings


def _mcp_seam_finding(call_mcp: object) -> Finding:
    label = f"PluginContext.call_mcp(..., {REQUIRED_MCP_KWARG})"
    if not callable(call_mcp):
        return Finding("seams", None, "fail", f"{label}: call_mcp missing")
    try:
        parameters = inspect.signature(call_mcp).parameters
    except (TypeError, ValueError) as exc:
        return Finding("seams", None, "fail", f"{label}: signature unreadable ({exc})")
    if REQUIRED_MCP_KWARG not in parameters:
        return Finding("seams", None, "fail", f"{label}: parameter missing")
    return Finding("seams", None, "ok", label)


def _hooks_seam_finding(valid_hooks: object) -> Finding:
    if not isinstance(valid_hooks, (set, frozenset)):
        return Finding("seams", None, "fail", "VALID_HOOKS is missing from hermes_cli.plugins")
    missing = [hook for hook in REQUIRED_HOOKS if hook not in valid_hooks]
    if missing:
        return Finding("seams", None, "fail", f"VALID_HOOKS is missing {', '.join(missing)}")
    return Finding("seams", None, "ok", f"VALID_HOOKS carries {', '.join(REQUIRED_HOOKS)}")


def _module_seam_finding(name: str) -> Finding:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        return Finding("seams", None, "fail", f"module {name} not importable ({exc})")
    if spec is None:
        return Finding("seams", None, "fail", f"module {name} not importable")
    return Finding("seams", None, "ok", f"module {name} importable")


def _host_plugins_module() -> ModuleType | None:
    try:
        return importlib.import_module("hermes_cli.plugins")
    except ImportError as exc:
        logger.warning("plugins-doctor: host plugin module unavailable: %s", exc)
        return None


def digest_plugin(plugin_dir: Path) -> str:
    """sha256 over sorted (relative path, bytes) of every ``*.py`` and the manifest file."""
    hasher = hashlib.sha256()
    for path in _source_files(plugin_dir):
        hasher.update(path.relative_to(plugin_dir).as_posix().encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _source_files(plugin_dir: Path) -> tuple[Path, ...]:
    candidates = {*(plugin_dir / name for name in MANIFEST_FILENAMES), *plugin_dir.rglob(SOURCE_PATTERN)}
    return tuple(
        sorted(
            (path for path in candidates if "__pycache__" not in path.parts and path.is_file()),
            key=lambda path: path.as_posix(),
        )
    )


def digest_plugins(entries: Sequence[PluginEntry]) -> dict[str, str]:
    """Digest each entry; an unreadable plugin is omitted (the stale check reports it as absent)."""
    digests: dict[str, str] = {}
    for entry in entries:
        try:
            digests[entry.name] = digest_plugin(entry.path)
        except OSError as exc:
            logger.warning("plugins-doctor: cannot digest %s: %s", entry.path, exc)
    return digests


def process_alive(pid: int) -> bool:
    """Whether *pid* still exists: ``/proc/<pid>`` on Linux, else a zero-signal probe.

    A pid can be reused by an unrelated process, so a live record is evidence of a possibly
    stale import, not proof of which process holds it — the reason each row names the argv0.
    """
    if pid <= 0:
        return False
    if _PROC_ROOT.is_dir():
        return (_PROC_ROOT / str(pid)).exists()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def parse_load_record(raw: object) -> LoadRecord | None:
    """Parse one persisted record; ``None`` for anything that is not a v1 record."""
    if not isinstance(raw, Mapping) or raw.get("v") != RECORD_VERSION:
        return None
    pid = raw.get("pid")
    argv0 = raw.get("argv0")
    started_at = raw.get("started_at")
    plugins = _as_mapping(raw.get("plugins"))
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(argv0, str) or not isinstance(started_at, str):
        return None
    if plugins is None:
        return None
    pairs = tuple(
        (name, digest)
        for name, digest in sorted(plugins.items())
        if isinstance(name, str) and isinstance(digest, str)
    )
    return LoadRecord(pid, argv0[:MAX_ARGV_CHARS], started_at, pairs)


def check_stale(
    records: Sequence[object], plugins_dir: Path, profile_plugins_dir: Path | None = None
) -> tuple[Finding, ...]:
    """(d) Live processes whose recorded plugin digests no longer match the source on disk.

    Nothing hot-reloads (README.md:59), so a live pid holding an older digest is running code the
    operator has since edited. Dead pids are pruned: their imports died with them.
    """
    directories = tuple(path for path in (plugins_dir, profile_plugins_dir) if path is not None)
    if not any(directory.is_dir() for directory in directories):
        return (
            Finding("stale_import", None, "warn", "plugins directory missing; stale-import check skipped"),
        )
    current = digest_plugins(merged_entries(directories))
    findings: list[Finding] = []
    live = dead = malformed = 0
    for raw in records:
        record = parse_load_record(raw)
        if record is None:
            malformed += 1
            continue
        if not process_alive(record.pid):
            dead += 1
            continue
        live += 1
        findings.extend(_stale_rows(record, current))
    summary = f"{live} live / {dead} dead load record(s)"
    if malformed:
        summary += f"; {malformed} malformed record(s) skipped"
        logger.warning("plugins-doctor: %d malformed load record(s) skipped", malformed)
    findings.append(Finding("stale_import", None, "warn" if malformed else "ok", summary))
    return tuple(findings)


def _stale_rows(record: LoadRecord, current: Mapping[str, str]) -> list[Finding]:
    rows: list[Finding] = []
    for name, loaded in record.plugins:
        present = current.get(name)
        if present == loaded:
            continue
        now = present[:DIGEST_PREVIEW_CHARS] if present else "absent (plugin removed or unreadable)"
        rows.append(
            Finding(
                "stale_import",
                name,
                "fail",
                f"pid {record.pid} ({record.argv0 or 'unknown argv'}) imported "
                f"{loaded[:DIGEST_PREVIEW_CHARS]}, source is now {now}",
            )
        )
    return rows
