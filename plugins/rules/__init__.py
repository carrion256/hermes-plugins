"""Rules — Cursor/Windsurf-style rule files via native Hermes seams.

Discovery is per session from real identity, never process cwd alone:
configured global dirs first, then project dirs (``.cursor/rules``,
``.windsurf/rules``, ``.claude/rules``) discovered by walking up from the
session's own cwd — nearest directory wins per rule name (ascending
priority: global < distant ancestor < project).

Front matter is parsed with PyYAML (comments, real lists); malformed or
unknown metadata never applies broadly. Bucketing (deterministic):
disabled/``enabled: false`` drop -> ``agents``-glob vs the session's
profile_name (unknown identity: agent-named rules stay inactive) ->
``alwaysApply`` -> rulebook; rules scoped by ``tools``/``condition``/``globs``
are one-time reminders. Reminder matching is AND over the present scopes.

Native seams only — tool arguments and file bytes are never mutated:
- per-session activation is seeded when the cache-safe system-prompt
  sections render (once per new session, real ``session_info`` identity);
- one-time rule guidance is delivered through tool_execution middleware: the
  short-circuit result carries the full rule bodies + file path in a canonical
  tool-error envelope. The real pipeline (agent/tool_executor.py) wraps this
  middleware around pre-tool policy: the plugin pre-tool block runs strictly
  below it, so guidance is delivered first, no pre-tool decision is consumed,
  and the model's retry meets pre-tool policy as normal; executor/args untouched;
- identity for resumed/frozen sessions is lazily seeded at tool-execution time
  from the real bound runtime (cwd via agent.runtime_cwd, profile via
  hermes_cli.profiles); unknown identity delivers no guidance;
- fired is marked only for rules actually delivered; activation state is
  dropped on ``on_session_reset`` / ``on_session_finalize`` (bounded LRU).
"""

from __future__ import annotations

import fnmatch
import logging
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_FM_RX = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TRUE, _FALSE = {"true", "yes", "on", "1"}, {"false", "no", "off", "0"}
_RULE_SUFFIXES = (".md", ".mdc")
_PROJECT_RULE_DIRS = (".cursor/rules", ".windsurf/rules", ".claude/rules")
_PATH_KEYS = ("path", "file_path", "file", "notebook_path", "target")
_BODY_BUDGET = 3600  # headroom under the native 4000-char section cap
_BOOKLET_BUDGET = 3600
_MAX_SESSIONS = 512
_META_KEYS = {"description", "alwaysapply", "always_apply", "enabled", "agents",
              "globs", "condition", "tools", "applies"}


class _Rule:
    __slots__ = ("name", "path", "description", "always", "enabled",
                 "agents", "globs", "condition", "tools", "body")

    def __init__(self, name: str, path: str, body: str):
        self.name, self.path, self.body = name, path, body
        self.description = ""
        self.always = False
        self.enabled = True
        self.agents: list[str] = []
        self.globs: list[str] = []
        self.condition: Optional[re.Pattern] = None
        self.tools: set[str] = set()


@dataclass
class _Activation:
    always: tuple[_Rule, ...]
    rulebook: tuple[_Rule, ...]
    reminders: tuple[_Rule, ...]
    fired: set = field(default_factory=set)
    cwd: str = ""


def _as_str_list(value: Any) -> Optional[list[str]]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return None
    if any(not isinstance(v, str) or not v.strip() for v in value):
        return None
    return [v.strip() for v in value]


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
    return None


def _load_rule_file(path: str) -> Optional[_Rule]:
    """Parse one rule file. Returns None (with a warning) for malformed or
    unsupported metadata — a bad rule must never silently apply broadly."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("rules: cannot read %s: %s", path, exc)
        return None
    m = _FM_RX.match(text)
    meta: dict = {}
    body = text.strip()
    if m:
        body = text[m.end():].strip()
        try:
            loaded = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as exc:
            logger.warning("rules: %s has malformed front matter: %s", path, exc)
            return None
        if not isinstance(loaded, dict):
            logger.warning("rules: %s front matter is not a mapping; ignoring file", path)
            return None
        meta = {str(k).lower(): v for k, v in loaded.items()}
    unknown = set(meta) - _META_KEYS
    if unknown:
        logger.warning("rules: %s has unknown metadata %s; ignoring file", path, sorted(unknown))
        return None
    if not body:
        return None
    rule = _Rule(PurePosixPath(path).stem, path, body)

    if "description" in meta:
        if not isinstance(meta["description"], str):
            logger.warning("rules: %s has non-string description; ignoring file", path)
            return None
        rule.description = meta["description"].strip()
    if "enabled" in meta:
        enabled = _as_bool(meta["enabled"])
        if enabled is None:
            logger.warning("rules: %s has malformed 'enabled'; ignoring file", path)
            return None
        rule.enabled = enabled
    always_raw = meta.get("alwaysapply", meta.get("always_apply"))
    if always_raw is not None:
        always = _as_bool(always_raw)
        if always is None:
            logger.warning("rules: %s has malformed 'alwaysApply'; ignoring file", path)
            return None
        rule.always = always
    elif meta.get("applies") == "always":  # legacy dialect
        rule.always = True
    for key, attr in (("agents", "agents"), ("globs", "globs")):
        if key in meta:
            items = _as_str_list(meta[key])
            if items is None:
                logger.warning("rules: %s has malformed '%s'; ignoring file", path, key)
                return None
            setattr(rule, attr, items)
    if "tools" in meta:
        items = _as_str_list(meta["tools"])
        if items is None:
            logger.warning("rules: %s has malformed 'tools'; ignoring file", path)
            return None
        rule.tools = set(items)
    if "condition" in meta:
        cond = meta["condition"]
        if not isinstance(cond, str):
            logger.warning("rules: %s has non-string condition; ignoring file", path)
            return None
        try:
            rule.condition = re.compile(cond)
        except re.error as exc:
            logger.warning("rules: %s has invalid condition %r: %s", path, cond, exc)
            return None
    return rule


def _scan_dir(directory: str) -> dict[str, _Rule]:
    found: dict[str, _Rule] = {}
    for fn in sorted(os.listdir(directory)):
        if fn.endswith(_RULE_SUFFIXES):
            parsed = _load_rule_file(os.path.join(directory, fn))
            if parsed is not None:
                found[parsed.name] = parsed
    return found


def _profile_name() -> str:
    """Agent identity bound to the current execution (contextvar-scoped)."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        return str(get_active_profile_name() or "")
    except Exception:
        return ""


def _runtime_cwd() -> str:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return str(resolve_agent_cwd() or "")
    except Exception:
        return ""

def register(ctx) -> None:
    global_dirs = [os.path.abspath(os.path.expanduser(str(raw)))
                   for raw in (ctx.get_config("rule_dirs") or ["~/.hermes/rules"])]
    disabled = {str(d) for d in (ctx.get_config("disabled_rules") or [])}
    ttsr_enabled = bool(ctx.get_config("ttsr_enabled", True))
    base_rules: dict[str, _Rule] = {}
    for d in global_dirs:
        if os.path.isdir(d):
            base_rules.update(_scan_dir(d))

    # Per-cwd merged discovery: global < ancestor < project (nearest wins).
    discovered: "OrderedDict[str, dict]" = OrderedDict()

    def _project_dirs(cwd: str) -> list[str]:
        found, cur, parent = [], cwd, None
        while cur and cur != parent and os.path.isdir(cur):
            for name in _PROJECT_RULE_DIRS:
                cand = os.path.join(cur, name)
                if os.path.isdir(cand):
                    found.append(cand)
            parent, cur = cur, os.path.dirname(cur)
        found.reverse()  # farthest ancestor first, session cwd's dirs last
        return found

    def _rules_for(cwd: str) -> dict:
        if cwd in discovered:
            discovered.move_to_end(cwd)
            return discovered[cwd]
        rules = dict(base_rules)
        for d in _project_dirs(cwd):
            rules.update(_scan_dir(d))
        while len(discovered) > _MAX_SESSIONS:
            discovered.popitem(last=False)
        discovered[cwd] = rules
        return rules

    # Session activation, seeded once per new session from real identity.
    activation: "OrderedDict[str, _Activation]" = OrderedDict()

    def _seed(info: Any) -> None:
        session_id = str(info.get("session_id") or "")
        if not session_id or session_id in activation:
            return
        cwd = str(info.get("cwd") or "")
        profile = str(info.get("profile_name") or "")
        rules = _rules_for(cwd)
        active: dict[str, _Rule] = {}
        for name in sorted(rules):
            rule = rules[name]
            if rule.enabled is False or name in disabled:
                continue
            # Unknown agent identity: rules that name agents stay inactive.
            if rule.agents and (not profile or not any(
                    fnmatch.fnmatch(profile, a) for a in rule.agents)):
                continue
            active[name] = rule
        reminders = tuple(r for r in active.values()
                          if r.tools or r.condition or r.globs)
        act = _Activation(
            always=tuple(r for r in active.values() if r.always and not r.globs),
            rulebook=tuple(r for r in active.values()
                           if r.description and (not r.always or r.globs)),
            reminders=reminders,
            cwd=cwd,
        )
        while len(activation) > _MAX_SESSIONS:
            activation.popitem(last=False)
        activation[session_id] = act
        demoted = len([r for r in active.values() if r.always and r.globs])
        logger.info("rules: session %s active: %d always, %d rulebook, %d reminder "
                    "(cwd=%s, %d alwaysApply+globs demoted to scoped reminder/rulebook)",
                    session_id, len(act.always), len(act.rulebook), len(act.reminders),
                    cwd or "-", demoted)

    def _render_always(info: Any) -> str:
        _seed(info)
        act = activation.get(str(info.get("session_id") or ""))
        if act is None:
            return ""
        omitted = 0
        text = ""
        for rule in act.always:
            candidate = (text + "\n\n" + rule.body) if text else rule.body
            if len(candidate) > _BODY_BUDGET:
                omitted += 1
                continue
            text = candidate
        if omitted:
            logger.warning("rules: %d always-apply rule(s) exceed the prompt budget "
                           "and are omitted for session %s", omitted, info.get("session_id"))
        return text

    def _render_rulebook(info: Any) -> str:
        _seed(info)
        act = activation.get(str(info.get("session_id") or ""))
        if act is None:
            return ""
        lines, omitted = [], []
        text = ""
        for rule in act.rulebook:
            line = (f"- **{rule.name}** — {rule.description}"
                    + (f" (globs: {', '.join(rule.globs)})" if rule.globs else "")
                    + f" [{rule.path}]")
            candidate = (text + "\n" + line) if text else line
            if len(candidate) > _BOOKLET_BUDGET:
                omitted.append(rule.name)
                continue
            text = candidate
            lines.append(line)
        if omitted:
            text += (f"\n- {', '.join(omitted)} — over the rulebook budget; "
                     "read these rule files directly")
        return text

    def _matches(rule: _Rule, tool_name: str, str_args: list[str],
                 resolved_paths: list[str], cwd: str) -> bool:
        # AND over present scopes: no single scope may act as a bypass.
        if rule.tools and tool_name not in rule.tools:
            return False
        if rule.condition and not any(rule.condition.search(v) for v in str_args):
            return False
        # Multi-segment globs are scoped to paths relative to the session
        # project root; an absolute suffix match would fire across foreign
        # workspaces. Slash-free globs (e.g. "*.rs") match the basename.
        if rule.globs:
            if not cwd:
                return False
            for p in resolved_paths:
                try:
                    rel = str(Path(p).relative_to(cwd))
                except ValueError:
                    continue
                if any("/" in g and PurePosixPath(rel).match(g) or
                       "/" not in g and fnmatch.fnmatch(
                           PurePosixPath(p).name, g)
                       for g in rule.globs):
                    return True
            return False
        return True

    def _resolve_path(value: str, task_id: str) -> str:
        try:
            from tools.file_tools_paths import _resolve_path_for_task
            return str(_resolve_path_for_task(value, task_id or "default"))
        except Exception:
            return ""

    def tool_execution(tool_name: str = "", args: Any = None, next_call=None,
                       session_id: str = "", task_id: str = "", **_kw):
        # One-time rule guidance delivered as the tool result itself. This
        # middleware wraps pre-tool policy (agent/tool_executor.py), so the
        # short-circuit delivers guidance first without consuming any pre-tool
        # decision; the retry meets pre-tool policy, and until it is reached
        # the executor never runs and args are never modified.
        if next_call is None or not ttsr_enabled or not isinstance(args, dict):
            return next_call(args) if next_call else None
        session_id = str(session_id or "")
        act = activation.get(session_id)
        if act is None:
            # Frozen-prompt / resumed sessions bypass section render: lazily
            # seed from the real bound runtime identity (same cwd resolution
            # the session's own prompt uses; agent scope stays unknown).
            if not session_id:
                return next_call(args)
            _seed({"session_id": session_id, "profile_name": _profile_name(),
                   "cwd": _runtime_cwd()})
            act = activation.get(session_id)
        hits = []
        str_args = [v for v in args.values() if isinstance(v, str)]
        paths = [args[k] for k in _PATH_KEYS if isinstance(args.get(k), str)]
        if tool_name == "patch" and args.get("mode") == "patch" and isinstance(args.get("patch"), str):
            from tools.file_tools import _collect_v4a_header_paths
            headers = _collect_v4a_header_paths(args["patch"])
            if not isinstance(headers, str):  # Native dispatch reports malformed paths.
                paths.extend(headers[0])
        resolved_paths = [resolved for path in paths
                          if (resolved := _resolve_path(path, task_id))]
        for rule in act.reminders:
            if rule.name in act.fired:  # already delivered once
                continue
            if _matches(rule, tool_name, str_args, resolved_paths, act.cwd):
                hits.append(rule)
        if not hits:
            return next_call(args)
        for rule in hits:  # mark fired only for actually delivered rules
            act.fired.add(rule.name)
        # Canonical tool-error envelope so the result reads as blocked; the
        # model retries the same call. Full bodies + file path, never truncated.
        text = "\n\n".join(f"{r.body}\n[rule file: {r.path}]" for r in hits)
        return json.dumps({
            "error": "Blocked once by rules plugin for review: "
                     + ", ".join(r.name for r in hits),
            "rule_guidance": text,
        }, ensure_ascii=False)

    def _drop_session(session_id: str = "", **_kw):
        activation.pop(str(session_id or ""), None)



    ctx.register_system_prompt_section("rules.always-apply", _render_always, max_chars=4000)
    ctx.register_system_prompt_section("rules.rulebook", _render_rulebook, max_chars=4000)
    ctx.register_middleware("tool_execution", tool_execution)
    ctx.register_hook("on_session_reset", _drop_session)
    ctx.register_hook("on_session_finalize", _drop_session)
