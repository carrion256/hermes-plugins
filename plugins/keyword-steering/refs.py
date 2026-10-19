"""Resolve keyword-steering `refs` (skill://, rule://, paths) against rule/skill roots.

Used at registration only, to warn about references that point nowhere.
Resolution never raises: any failure means "unresolved".
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

DEFAULT_ROOTS = [
    "~/.hermes/rules",
    "~/.omp/rules",
    "~/.omp/agent/skills",
    "~/.omp/agent/managed-skills",
]


def roots_from_config(ctx) -> list[str]:
    configured = ctx.get_config("ref_roots", DEFAULT_ROOTS)
    if not isinstance(configured, (list, tuple)):
        configured = DEFAULT_ROOTS
    roots = [r for r in configured if isinstance(r, str) and r.strip()]
    env = os.environ.get("OMP_RULES", "").strip()
    if env:
        roots.append(env)
    seen: list[str] = []
    for root in roots:
        expanded = os.path.expanduser(root)
        if expanded not in seen:
            seen.append(expanded)
    return seen


def _first_file(pattern: str) -> Path | None:
    for hit in sorted(glob.glob(pattern)):
        if os.path.isfile(hit):
            return Path(hit)
    return None


def _skill_roots(roots: list[str]) -> list[str]:
    extra: list[str] = []
    try:
        from hermes_constants import get_skills_dir

        extra.append(str(get_skills_dir()))
    except Exception:
        pass
    return extra + list(roots)


def _resolve(ref: str, roots: list[str]) -> Path | None:
    ref = ref.strip()
    if not ref:
        return None
    scheme, sep, rest = ref.partition(":")
    if scheme in ("skill", "rule") and (rest.startswith("//") or sep):
        name = rest[2:] if rest.startswith("//") else rest
        kind = scheme
    elif "://" not in ref and "/" not in ref and os.sep not in ref:
        name, kind = ref, "rule"
    else:
        name, kind = "", "path"

    if kind == "skill":
        if not name:
            return None
        esc = glob.escape(name)
        for root in _skill_roots(roots):
            base = glob.escape(root)
            for pattern in (
                f"{base}/{esc}/SKILL.md",
                f"{base}/*/{esc}/SKILL.md",
                f"{base}/*/*/{esc}/SKILL.md",
                f"{base}/{esc}.md",
            ):
                hit = _first_file(pattern)
                if hit:
                    return hit
        return None

    if kind == "rule":
        if not name:
            return None
        for root in roots:
            for suffix in ("", ".md", ".mdc"):
                candidate = Path(root) / (name + suffix)
                if candidate.is_file():
                    return candidate
        return None

    path = Path(ref).expanduser()
    if path.is_absolute():
        return path if path.exists() else None
    for root in roots:
        candidate = Path(root) / path
        if candidate.exists():
            return candidate
    return path if path.exists() else None


def resolve_ref(ref: str, roots: list[str]) -> str | None:
    try:
        hit = _resolve(ref, roots)
        return str(hit.resolve()) if hit else None
    except Exception:
        return None


def unresolved_refs(modes: dict, roots: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, spec in modes.items():
        missing = [r for r in spec.get("refs", ()) if resolve_ref(r, roots) is None]
        if missing:
            out[name] = missing
    return out
