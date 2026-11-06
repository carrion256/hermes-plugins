"""plugins-doctor: a read-only health report for the plugin estate.

The installation contract is "link a directory containing ``plugin.yaml`` into
``$HERMES_HOME/plugins``, enable it, configure it", and the README's native-prerequisites section
is prose. Both drift silently. This plugin turns them into rows instead of advice:

- ``links``        - every entry under the plugins dir (and the profile's) resolves to a manifest
- ``settings``     - enabled plugins' settings, validated through the host's own ``config_schema``
- ``seams``        - the host contracts README.md:51-57 promise are actually present here
- ``stale_import`` - live processes still importing plugin source whose bytes have since changed

It repairs nothing and rewrites nothing outside its own load records: an operator decides. The
CLI exits 1 only when a row is a ``fail``; ``--json`` prints the same rows as an array.

Load records are what makes the stale check possible. ``register`` runs in every process that
loads plugins — the CLI, the gateway, a kanban worker — and records the sha256 of each plugin
entry's source at that moment. Editing source or configuration does not hot-reload an already
imported module (README.md:59), so a still-live pid holding an older digest is running code the
operator has since replaced.
"""
from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from hermes_constants import get_hermes_home

from . import checks
from .checks import Finding

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

logger = logging.getLogger(__name__)

DEFAULT_RECORDS_KEEP: Final[int] = 16
_TABLE_HEADER: Final[str] = f"{'CHECK':<14}{'PLUGIN':<30}{'SEVERITY':<9}DETAIL"


@dataclass(frozen=True, slots=True)
class DoctorSettings:
    """The plugin's own settings, read once at register."""

    stale_records_keep: int


def register(ctx: PluginContext) -> None:
    """Record this process's load, then expose ``hermes plugins-doctor``. No tools, no hooks."""
    settings = _read_settings(ctx)
    record_load(ctx, settings, _profile_dirs())
    ctx.register_cli_command(
        name="plugins-doctor",
        help="Report plugin link, settings, host-seam and stale-import problems (read-only).",
        setup_fn=_setup_cli,
        handler_fn=functools.partial(_handle_cli, ctx=ctx),
    )


def _read_settings(ctx: PluginContext) -> DoctorSettings:
    """Read ``stale_records_keep``; an invalid value is a documented fallback, never a crash.

    Refusing to register would silence every other row this plugin exists to print, so a bad
    value warns once and the default is used.
    """
    raw = ctx.get_config("stale_records_keep", DEFAULT_RECORDS_KEEP)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        logger.warning(
            "plugins-doctor: stale_records_keep must be a positive int, got %r; using %d",
            raw,
            DEFAULT_RECORDS_KEEP,
        )
        return DoctorSettings(DEFAULT_RECORDS_KEEP)
    return DoctorSettings(raw)


def record_load(ctx: PluginContext, settings: DoctorSettings, directories: Sequence[Path]) -> None:
    """Persist this process's source digests under ``ctx.state['loads']``; never raises.

    A plugin whose source cannot be read simply omits its digest, so the doctor reports it as
    absent rather than losing the whole record.
    """
    state = getattr(ctx, "state", None)
    if state is None:
        logger.warning("plugins-doctor: load record not written (no state store on this context)")
        return
    try:
        records = _prior_records(state.get(checks.LOAD_RECORDS_KEY, []))
        records.append(_new_record(directories))
        state.set(checks.LOAD_RECORDS_KEY, records[-settings.stale_records_keep :])
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("plugins-doctor: load record not written (%s)", exc)


def _prior_records(stored: object) -> list[dict[str, object]]:
    """Kept records, dropping any row this pid already wrote: a reload re-snapshots itself."""
    if not isinstance(stored, list):
        return []
    return [dict(item) for item in stored if isinstance(item, Mapping) and item.get("pid") != os.getpid()]


def _new_record(directories: Sequence[Path]) -> dict[str, object]:
    return {
        "v": checks.RECORD_VERSION,
        "pid": os.getpid(),
        "argv0": (sys.argv[0] if sys.argv else "")[: checks.MAX_ARGV_CHARS],
        "started_at": datetime.now(UTC).isoformat(),
        "plugins": checks.digest_plugins(checks.merged_entries(directories)),
    }


def run_checks(ctx: PluginContext) -> tuple[Finding, ...]:
    """All four checks, in the order an operator would repair them."""
    directories = _profile_dirs()
    primary = directories[0]
    profile_dir = directories[1] if len(directories) > 1 else None
    findings: list[Finding] = list(checks.check_links(primary, profile_dir))
    config, config_error = _read_config()
    if config_error is not None:
        findings.append(Finding("settings", None, "fail", config_error))
    manifests, manifest_error = _read_manifests(directories)
    if manifest_error is not None:
        findings.append(Finding("settings", None, "fail", manifest_error))
    findings.extend(checks.check_settings(config, manifests))
    findings.extend(checks.check_seams())
    records, records_error = _stored_records(ctx)
    if records_error is not None:
        findings.append(Finding("stale_import", None, "fail", records_error))
    findings.extend(checks.check_stale(records, primary, profile_dir))
    return tuple(findings)


def _profile_dirs() -> tuple[Path, ...]:
    """The plugins dirs of the active home, plus the named profile's when one is set."""
    return checks.plugin_dirs(get_hermes_home(), os.environ.get("HERMES_PROFILE"))


def _read_config() -> tuple[Mapping[str, object], str | None]:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except (ImportError, OSError, ValueError) as exc:
        return {}, f"profile config unreadable ({exc.__class__.__name__}: {exc})"
    if not isinstance(config, Mapping):
        return {}, "profile config is not a mapping"
    return config, None


def _read_manifests(
    directories: Sequence[Path],
) -> tuple[dict[str, checks.ManifestLike], str | None]:
    """Manifests by host registry key (``cat/name`` for category plugins), through the host's
    own discovery so schema normalisation matches what the loader sees."""
    try:
        from hermes_cli.plugins_discovery import manifest_key, scan_directory
    except ImportError as exc:
        return {}, f"host plugin discovery unavailable ({exc.__class__.__name__}: {exc})"
    manifests: dict[str, checks.ManifestLike] = {}
    for directory in directories:
        try:
            for manifest in scan_directory(directory, "user"):
                manifests.setdefault(manifest_key(manifest), manifest)
        except (OSError, ValueError, TypeError) as exc:
            return manifests, f"cannot scan {directory} for manifests ({exc.__class__.__name__}: {exc})"
    return manifests, None


def _stored_records(ctx: PluginContext) -> tuple[list[object], str | None]:
    """Persisted load records; an unreadable state file is a row, never a silent empty list."""
    state = getattr(ctx, "state", None)
    if state is None:
        return [], "load records unavailable (no state store on this context)"
    try:
        stored = state.get(checks.LOAD_RECORDS_KEY, [])
    except (OSError, RuntimeError, ValueError) as exc:
        return [], f"load records unreadable ({exc.__class__.__name__}: {exc})"
    return (stored, None) if isinstance(stored, list) else ([], "load records are not a list")


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the findings as a JSON array instead of a table",
    )


def _handle_cli(args: argparse.Namespace, *, ctx: PluginContext) -> int:
    """Print every finding; exit 1 only when one of them is a ``fail``."""
    rows = checks.ordered_findings(run_checks(ctx))
    if args.json:
        print(json.dumps([row.as_dict() for row in rows], indent=2))
    else:
        print(_TABLE_HEADER)
        for row in rows:
            print(f"{row.check:<14}{row.plugin or '-':<30}{row.severity:<9}{row.detail}")
    return 1 if checks.has_failure(rows) else 0
