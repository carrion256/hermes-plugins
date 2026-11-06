"""Papercut → inert rule drafts: recurring grievances a human can adopt.

The native binary owns the record (identity, recurrence count, status). This module
turns a *recurring* papercut into a rule file that changes nothing by itself:
front matter carries ``enabled: false``, so the rules plugin treats it as inert even
if the directory were scanned — and it is not, because rule discovery reads only the
direct ``*.md`` files of each configured rule dir (``plugins/rules/__init__.py``
``_scan_dir`` uses ``os.listdir`` and never descends). Adopting a draft is an
explicit human act: move it into a scanned dir, set ``enabled: true``, add the
``globs``/``tools`` scope. Nothing self-activates.

Parsing happens once, at the boundary: ``parse_list_rows`` consumes native
``list --json`` stdout and produces frozen ``PapercutEntry`` values; the rest of the
module never sees a dict. An unknown row shape is skipped and COUNTED (the count
rides back to the caller, which surfaces it) rather than guessed at; a payload that
is not native list JSON raises ``ValueError`` instead of silently producing nothing.

No logger here: structlog is not a host dependency and stdlib logging is disallowed
in new modules by house rules, so every degraded path is a return value or a raise.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NewType, TypeAlias

import yaml

SHARED_MODULE: Final = "hermes_plugins_papercuts_drafts"

# The native record's own stable identity (its ``id``, which dedup keeps across
# recurrences, or a ``fingerprint`` column if a build ever adds one). Not a
# generic string: it names a file on disk.
Fingerprint: TypeAlias = NewType("Fingerprint", str)

DRAFT_PREFIX: Final = "papercut-"
DRAFT_SUFFIX: Final = ".md"
FINGERPRINT_LIMIT: Final = 80
SUMMARY_LIMIT: Final = 200
DESCRIPTION_LIMIT: Final = 300
_ROW_LIST_KEYS: Final = ("items", "records", "papercuts")
_SAFE_CHARS: Final[frozenset[str]] = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


@dataclass(frozen=True, slots=True)
class PapercutEntry:
    """One native papercut row, already parsed.

    ``fingerprint`` is the native record's own stable identity (its ``id``, which
    dedup preserves across recurrences, or a ``fingerprint`` field if a future
    build supplies one). ``expected``/``observed``/``repro`` come from explicit
    columns when a build has them, otherwise from the ``context`` lines the
    papercuts plugin writes.
    """

    fingerprint: Fingerprint
    summary: str
    count: int
    category: str
    severity: str
    expected: str
    observed: str
    repro: str


@dataclass(frozen=True, slots=True)
class DraftReport:
    """What ``emit_drafts`` did — counts, so a refusal is never invisible."""

    written: tuple[Path, ...]
    skipped_existing: int
    below_threshold: int


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _count(value: object) -> int | None:
    """Native recurrence count: a positive integer, or None (unknown shape)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    number = int(value)
    return number if number >= 1 else None


def _context_value(context: str, name: str) -> str:
    """Recover ``name: value`` from the native ``context`` blob, if present."""
    prefix = f"{name}:"
    for line in context.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(prefix):
            return stripped[len(prefix):].strip()
    return ""


def _entry(row: object) -> PapercutEntry | None:
    if not isinstance(row, dict):
        return None
    fingerprint = _text(row.get("fingerprint")) or _text(row.get("id"))
    count = _count(row.get("count", 1))
    if not fingerprint or count is None:
        return None
    context = _text(row.get("context"))
    return PapercutEntry(
        fingerprint=Fingerprint(fingerprint),
        summary=_text(row.get("summary")),
        count=count,
        category=_text(row.get("category")),
        severity=_text(row.get("severity")),
        expected=_text(row.get("expected")) or _context_value(context, "expected"),
        observed=_text(row.get("observed")) or _context_value(context, "observed"),
        repro=_text(row.get("repro")) or _context_value(context, "repro"),
    )


def _rows(loaded: object) -> list[object]:
    if isinstance(loaded, list):
        return list(loaded)
    if isinstance(loaded, dict):
        for key in _ROW_LIST_KEYS:
            value = loaded.get(key)
            if isinstance(value, list):
                return list(value)
    raise ValueError("papercut list payload does not contain a row list")


def parse_list_rows(payload: str) -> tuple[tuple[PapercutEntry, ...], int]:
    """Parse native ``list --json`` stdout ONCE.

    Returns the usable entries plus the number of rows skipped for an unknown
    shape. Raises ``ValueError`` when the payload is not JSON at all or has no row
    list — the caller surfaces that; an empty result never reads as "nothing to do".
    """
    text = payload.strip()
    if not text:
        return (), 0
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"papercut list payload is not JSON: {exc}") from exc
    entries: list[PapercutEntry] = []
    skipped = 0
    for row in _rows(loaded):
        entry = _entry(row)
        if entry is None:
            skipped += 1
            continue
        entries.append(entry)
    return tuple(entries), skipped


def _safe_fingerprint(raw: Fingerprint) -> str:
    """Filesystem-safe, deterministic, never empty, never a traversal.

    Anything outside ``[A-Za-z0-9._-]`` becomes ``_``; a name that would collapse
    to punctuation only falls back to a digest so the record is still written
    rather than silently dropped.
    """
    stripped = raw.strip()
    cleaned = "".join(
        char if char.lower() in _SAFE_CHARS else "_" for char in stripped
    )[:FINGERPRINT_LIMIT]
    if cleaned and set(cleaned) != {"_"}:
        return cleaned
    return "fp" + hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16]


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _block(text: str) -> str:
    return text if text else "(not recorded)"


def draft_rule_markdown(entry: PapercutEntry) -> str:
    """Render one inert rule draft for a human to adopt. Pure."""
    fingerprint = _safe_fingerprint(entry.fingerprint)
    summary = _one_line(entry.summary, SUMMARY_LIMIT) or "recurring tool papercut"
    meta: dict[str, object] = {
        "id": f"{DRAFT_PREFIX}{fingerprint}",
        "description": _one_line(
            f"DRAFT from papercut {fingerprint}: {summary}", DESCRIPTION_LIMIT
        ),
        "enabled": False,
    }
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    body = "\n".join(
        (
            f"# Papercut draft: {summary}",
            "",
            f"- fingerprint: `{fingerprint}`",
            f"- category: {entry.category or 'unspecified'}",
            f"- severity: {entry.severity or 'unspecified'}",
            f"- occurrences: {entry.count}",
            "",
            "## Summary",
            summary,
            "",
            "## Expected",
            _block(entry.expected),
            "",
            "## Observed",
            _block(entry.observed),
            "",
            "## Repro",
            _block(entry.repro),
            "",
            "## Adopting this draft",
            "This draft is inert: `enabled: false` keeps it out of the rule set, and",
            "rule discovery reads only the direct `*.md` files of a configured rule",
            "dir, so this drafts directory is never scanned. To adopt it: move the",
            "file into a scanned rules directory, set `enabled: true`, and add the",
            "`globs`/`tools` scope this behaviour actually needs. Until then it is",
            "evidence, not policy.",
            "",
        )
    )
    return f"---\n{front}\n---\n\n{body}"


def _publish(path: Path, text: str) -> bool:
    """Create ``path`` atomically with ``text``; never overwrite. True when written.

    The temp file lives in the SAME directory (``os.replace``/``os.link`` are only
    atomic within one filesystem), and an existing draft wins: publication is a
    hard-link, so a concurrent emitter gets ``FileExistsError``, not a lost write.
    """
    descriptor, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        tmp.unlink(missing_ok=True)


def parse_threshold(raw: object) -> int:
    """Parse a recurrence threshold: an int >= 1, or a typed refusal.

    A non-integer is a TYPE error, an integer below 1 a VALUE error. Refusing
    loudly (never a silent default) is what keeps an inert feature from quietly
    doing something the operator did not ask for."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError("papercuts: draft_rule_after must be an integer")
    if raw < 1:
        raise ValueError("papercuts: draft_rule_after must be an integer >= 1")
    return raw


def emit_drafts(
    entries: Iterable[PapercutEntry], draft_dir: Path, threshold: int
) -> DraftReport:
    """Write drafts for entries at or above ``threshold`` recurrences.

    Never overwrites an existing draft (a human may have edited it) and leaves no
    temp files behind. ``threshold`` is a caller-supplied policy, parsed here so
    every entry point refuses it the same way.
    """
    target_dir = Path(draft_dir)
    threshold = parse_threshold(threshold)
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    skipped_existing = 0
    below_threshold = 0
    for entry in entries:
        if entry.count < threshold:
            below_threshold += 1
            continue
        path = target_dir / f"{DRAFT_PREFIX}{_safe_fingerprint(entry.fingerprint)}{DRAFT_SUFFIX}"
        if _publish(path, draft_rule_markdown(entry)):
            written.append(path)
        else:
            skipped_existing += 1
    return DraftReport(tuple(written), skipped_existing, below_threshold)


# Shared-slot registration, same contract as friction.py: path loaders check
# sys.modules first so one process never holds two copies of this module.
sys.modules.setdefault(SHARED_MODULE, sys.modules[__name__])
