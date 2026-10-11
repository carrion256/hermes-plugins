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
import hashlib
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
              "globs", "condition", "tools", "applies", "id", "check_refs",
              "verification_refs", "constraints"}


class _Rule:
    __slots__ = ("name", "path", "description", "always", "enabled", "id",
                 "agents", "globs", "condition", "tools", "body", "check_refs",
                 "verification_refs", "constraints")

    def __init__(self, name: str, path: str, body: str):
        self.name, self.path, self.body = name, path, body
        self.description = ""
        self.always = False
        self.enabled = True
        self.agents: list[str] = []
        self.globs: list[str] = []
        self.condition: Optional[re.Pattern] = None
        self.tools: set[str] = set()
        self.id = "rule:" + hashlib.sha256(os.fsencode(path)).hexdigest()[:24]
        self.check_refs: list[str] = []
        self.verification_refs: list[str] = []
        self.constraints: dict[str, str] = {}


@dataclass
class _Activation:
    always: tuple[_Rule, ...]
    rulebook: tuple[_Rule, ...]
    reminders: tuple[_Rule, ...]
    fired: set = field(default_factory=set)
    cwd: str = ""
    profile: str = ""


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
    except (OSError, UnicodeError) as exc:
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
    # References are opaque data: never import, resolve, or execute them.
    if "id" in meta:
        if not isinstance(meta["id"], str) or not 1 <= len(meta["id"].strip()) <= 256:
            logger.warning("rules: %s has malformed id; ignoring file", path)
            return None
        rule.id = meta["id"].strip()
    for key in ("check_refs", "verification_refs"):
        if key in meta:
            values = meta[key]
            if (not isinstance(values, list) or len(values) > 32 or
                    any(not isinstance(v, str) or not v.strip() or len(v) > 1024
                        for v in values)):
                logger.warning("rules: %s has malformed %s; ignoring file", path, key)
                return None
            setattr(rule, key, values)
    if "constraints" in meta:
        values = meta["constraints"]
        if (not isinstance(values, dict) or len(values) > 32 or
                any(not isinstance(k, str) or not k.strip() or len(k) > 256 or
                    not isinstance(v, str) or not v.strip() or len(v) > 1024
                    for k, v in values.items())):
            logger.warning("rules: %s has malformed constraints; ignoring file", path)
            return None
        rule.constraints = values

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
    explain_enabled = bool(ctx.get_config("rules_explain_enabled", True))
    base_rules: dict[str, _Rule] = {}
    base_sources: list[_Rule] = []
    for d in global_dirs:
        if os.path.isdir(d):
            found = _scan_dir(d)
            base_rules.update(found)
            base_sources.extend(found.values())

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

    def _rules_for(cwd: str) -> tuple[dict, list]:
        """(merged winners, source-ordered candidates) — ascending precedence:
        global dirs, farthest ancestor first, session cwd's dirs last."""
        if cwd in discovered:
            discovered.move_to_end(cwd)
            return discovered[cwd]
        rules = dict(base_rules)
        sources = list(base_sources)
        for d in _project_dirs(cwd):
            found = _scan_dir(d)
            rules.update(found)
            sources.extend(found.values())
        while len(discovered) > _MAX_SESSIONS:
            discovered.popitem(last=False)
        discovered[cwd] = (rules, sources)
        return discovered[cwd]

    # Session activation, seeded once per new session from real identity.
    activation: "OrderedDict[str, _Activation]" = OrderedDict()

    def _rule_meta(rule: _Rule) -> dict:
        return {"id": rule.id, "source": rule.path, "description": rule.description}

    def _rule_refs(rules: list[_Rule]) -> list[dict]:
        return [_rule_meta(r) for r in sorted(rules, key=lambda r: r.name)]

    def _extract_paths(tool_name: str, args: dict, task_id: str) -> list[str]:
        """Path args resolved against the session's task root; unresolvable
        values are skipped (foreign/unknown workspace delivers nothing)."""
        paths = [args[k] for k in _PATH_KEYS if isinstance(args.get(k), str)]
        if tool_name == "patch" and args.get("mode") == "patch" and isinstance(args.get("patch"), str):
            from tools.file_tools import _collect_v4a_header_paths
            headers = _collect_v4a_header_paths(args["patch"])
            if not isinstance(headers, str):  # Native dispatch reports malformed paths.
                paths.extend(headers[0])
        return [resolved for path in paths
                if (resolved := _resolve_path(path, task_id))]

    def _conflicts(rules: list[_Rule]) -> list[dict]:
        """Explicit conflicts only — same rule name with different content
        (precedence shadowing) or the same declared constraint key with
        different values. Both rule sources are reported; the winner is the
        higher-precedence rule (nearest directory wins)."""
        out: list[dict] = []
        by_name: dict[str, list[_Rule]] = {}
        for r in rules:
            by_name.setdefault(r.name, []).append(r)
        for name, group in sorted(by_name.items()):
            if len(group) < 2:
                continue
            winner = group[-1]
            for loser in group[:-1]:
                if loser.body != winner.body:
                    out.append({
                        "type": "shadowed",
                        "rule": name,
                        "reason": "same rule name, different content — the nearer source "
                                  "overrides (global < ancestor < project, nearest wins)",
                        "winner": _rule_meta(winner),
                        "loser": _rule_meta(loser),
                    })
        by_key: dict[str, list[tuple[_Rule, str]]] = {}
        for r in rules:
            for key, value in sorted(r.constraints.items()):
                by_key.setdefault(key, []).append((r, value))
        for key, pairs in sorted(by_key.items()):
            if len(pairs) < 2 or len({v for _, v in pairs}) < 2:
                continue  # declared equal values are agreement, not conflict
            winner, _ = pairs[-1]
            for loser, loser_value in pairs[:-1]:
                if loser_value != winner.constraints.get(key):
                    out.append({
                        "type": "declared_constraint",
                        "constraint": key,
                        "reason": "incompatible declared constraint values — "
                                  "the nearer source wins (global < ancestor < project)",
                        "winner": _rule_meta(winner) | {"value": winner.constraints[key]},
                        "loser": _rule_meta(loser) | {"value": loser_value},
                    })
        return out

    def _seed(info: Any) -> None:
        session_id = str(info.get("session_id") or "")
        if not session_id or session_id in activation:
            return
        cwd = str(info.get("cwd") or "")
        profile = str(info.get("profile_name") or "")
        rules = _rules_for(cwd)[0]
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
            profile=profile,
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
            return text + (f"\n\n[RULES] {omitted} always-apply rule(s) exceed the "
                           "prompt budget and are omitted; read the rule files directly.")
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
            shown = ", ".join(omitted[:8])
            more = f" (+{len(omitted) - 8} more)" if len(omitted) > 8 else ""
            text += (f"\n- {shown}{more} — over the rulebook budget; "
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
        # One-time rule guidance delivered WITH the executed result — no
        # synthetic error, no retry round-trip. This middleware wraps
        # pre-tool policy (agent/tool_executor.py), so the executor and
        # args are never modified; guidance rides the real tool result and
        # each rule is delivered at most once per session.
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
        resolved_paths = _extract_paths(tool_name, args, task_id)
        for rule in act.reminders:
            if rule.name in act.fired:  # already delivered once
                continue
            if _matches(rule, tool_name, str_args, resolved_paths, act.cwd):
                hits.append(rule)
        if not hits:
            return next_call(args)
        for rule in hits:  # mark fired only for actually delivered rules
            act.fired.add(rule.name)
        text = "\n\n".join(f"{r.body}\n[rule file: {r.path}]" for r in hits)
        refs = _rule_refs(hits)
        result = next_call(args)
        if not isinstance(result, str):
            return result  # structural result: never corrupt tool output
        note = "\n\n[RULE GUIDANCE — one-time, apply to this change]\n" + text
        if refs:
            note += "\n[rule refs] " + json.dumps(refs, ensure_ascii=False)
        # JSON tool results must remain JSON: native mutation tracking and
        # downstream consumers parse them. Attach guidance as data there;
        # only free-form text gets the appended note.
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            data["rule_guidance"] = text
            if refs:
                data["rule_refs"] = refs
            return json.dumps(data, ensure_ascii=False)
        return result + note

    def _drop_session(session_id: str = "", **_kw):
        activation.pop(str(session_id or ""), None)

    def _agent_ok(rule: _Rule, agent: str) -> bool:
        return not rule.agents or any(
            fnmatch.fnmatch(agent, a) for a in rule.agents)

    def _bucketed(active_rules: dict) -> tuple:
        """The same deterministic bucketing _seed applies, without session state."""
        reminders = tuple(r for r in active_rules.values()
                          if r.tools or r.condition or r.globs)
        always = tuple(r for r in active_rules.values()
                       if r.always and not r.globs)
        rulebook = tuple(r for r in active_rules.values()
                         if r.description and (not r.always or r.globs))
        return always, rulebook, reminders

    def _match_reasons(rule: _Rule, tool_name: str, resolved_paths: list[str],
                       agent: str) -> list[str]:
        reasons = []
        if rule.tools:
            reasons.append(f"tool scope {tool_name!r} matches rule tools "
                           + ", ".join(sorted(rule.tools)))
        if rule.condition:
            reasons.append("condition regex matched the intended arguments")
        if rule.globs:
            reasons.append("glob matched intended path: " + ", ".join(rule.globs))
        if rule.agents:
            reasons.append(f"agent scope matched {agent!r} "
                           f"(rule agents: {', '.join(rule.agents)})")
        return reasons

    def rules_explain(args: Any = None, session_id: str = "",
                      task_id: str = "", **_kw) -> str:
        # Read-only explanation of rule decisions for a planner BEFORE the
        # edit happens. Nothing is executed or mutated: check/verification
        # refs are returned as opaque data, never imported, resolved, or run.
        args = args if isinstance(args, dict) else {}
        paths = args.get("paths") or []
        tool_name = str(args.get("tool_name") or "")
        agent = str(args.get("agent") or "")
        if (not isinstance(paths, list)
                or len(paths) > 64
                or any(not isinstance(p, str) or not p.strip() for p in paths)):
            return json.dumps({"success": False,
                               "error": "paths must be a list of non-empty strings"})
        # Actual session CWD/identity: the seeded session wins; otherwise the
        # real runtime identity (same source the tool_execution lane uses).
        act = activation.get(str(session_id or ""))
        agent = agent or (act.profile if act else _profile_name())
        cwd = act.cwd if act else _runtime_cwd()
        resolved = [resolved for p in paths
                    if (resolved := _resolve_path(p, task_id))]
        str_args = [*resolved, tool_name]
        merged, sources = _rules_for(cwd)
        # Session-active winners (enabled/agent/disabled), source-ordered.
        active_rules: dict[str, _Rule] = {}
        for name, rule in sorted(merged.items()):
            if rule.enabled is False or name in disabled:
                continue
            if not _agent_ok(rule, agent):
                continue
            active_rules[name] = rule
        # Conflicts are computed from ENABLED, IN-SCOPE ORIGINAL SOURCES —
        # including precedence losers. Winner filtering first would delete
        # exactly the overridden side the conflict report exists to explain
        # (same-name shadowing, or identical bodies whose declared
        # constraints differ are not identical guidance).
        candidates = [r for r in sources
                      if r.enabled is not False and r.name not in disabled
                      and _agent_ok(r, agent)]
        always, rulebook, reminders = _bucketed(active_rules)
        # Deterministic AND-scoped matching, same engine the live lane uses.
        # always-apply rules are unconditional; globbed rulebook entries are
        # judged through the reminder lane below, plain rulebook entries are
        # indexed by description.
        matched = []
        for rule in always:
            matched.append({
                "id": rule.id, "name": rule.name, "source": rule.path,
                "reasons": ["alwaysApply: active for every session"],
                "check_refs": rule.check_refs,
                "verification_refs": rule.verification_refs,
            })
        for rule in reminders:
            if _matches(rule, tool_name, str_args, resolved, cwd):
                matched.append({
                    "id": rule.id, "name": rule.name, "source": rule.path,
                    "reasons": _match_reasons(rule, tool_name, resolved, agent),
                    "check_refs": rule.check_refs,
                    "verification_refs": rule.verification_refs,
                })
        matched_names = {m["name"] for m in matched}
        for rule in rulebook:
            if rule.globs or rule.tools or rule.condition:
                continue  # tool/condition/glob scopes are judged EXCLUSIVELY
                          # through the reminder lane — never re-added here
            if rule.name in matched_names:
                continue  # always entries deduped
            matched.append({
                "id": rule.id, "name": rule.name, "source": rule.path,
                "reasons": ["rulebook: indexed by description"],
                "check_refs": rule.check_refs,
                "verification_refs": rule.verification_refs,
            })
        matched.sort(key=lambda m: m["name"])
        # Budget omissions: exactly the rules the live renderer lanes drop.
        text, always_omitted = "", []
        for rule in always:
            cand = (text + "\n\n" + rule.body) if text else rule.body
            if len(cand) > _BODY_BUDGET:
                always_omitted.append(rule.name)
                continue
            text = cand
        text, booklet_omitted = "", []
        for rule in rulebook:
            line = (f"- **{rule.name}** — {rule.description}"
                    + (f" (globs: {', '.join(rule.globs)})" if rule.globs else "")
                    + f" [{rule.path}]")
            cand = (text + "\n" + line) if text else line
            if len(cand) > _BOOKLET_BUDGET:
                booklet_omitted.append(rule.name)
                continue
            text = cand
        # Precedence/shadowing: every source the discovery merge dropped.
        by_name: dict[str, list[_Rule]] = {}
        for r in sources:
            by_name.setdefault(r.name, []).append(r)
        shadowed = []
        for name, group in sorted(by_name.items()):
            winner = merged.get(name)
            if winner is None or name in disabled:
                continue
            for loser in group:
                if loser.path != winner.path:
                    shadowed.append({"rule": name, "source": loser.path,
                                     "overridden_by": winner.path,
                                     "identical_content": loser.body == winner.body})
        return json.dumps({
            "success": True,
            "cwd": cwd or None,
            "agent": agent or None,
            "resolved_paths": resolved,
            "matched": matched,
            "rule_refs": _rule_refs([r for r in active_rules.values()
                                     if r.name in {m["name"] for m in matched}]),
            "shadowed": shadowed,
            "conflicts": _conflicts([r for r in candidates
                                     if _matches(r, tool_name, str_args, resolved, cwd)]),
            "budget_omissions": {
                "always_apply": always_omitted,
                "rulebook": booklet_omitted,
            },
        }, ensure_ascii=False)

    if explain_enabled:
        ctx.register_tool(
            name="rules_explain", toolset="rules",
            schema={"name": "rules_explain",
                    "description": "Explain rule decisions for intended paths/tool/agent: "
                                   "applicable rules with stable ids and sources, why they "
                                   "matched, precedence/shadowing, explicit conflicts with "
                                   "both sources, and budget omissions. Read-only.",
                    "parameters": {"type": "object", "properties": {
                        "paths": {"type": "array", "items": {"type": "string"},
                                  "description": "Intended file paths for the next edit."},
                        "tool_name": {"type": "string",
                                      "description": "Intended tool (e.g. patch, terminal)."},
                        "agent": {"type": "string",
                                  "description": "Agent id to evaluate agent-scoped rules for."},
                    }}},
            handler=rules_explain,
            description="Explain which rules apply to intended paths/tool/agent before editing.",
        )

    ctx.register_system_prompt_section("rules.always-apply", _render_always, max_chars=4000)
    ctx.register_system_prompt_section("rules.rulebook", _render_rulebook, max_chars=4000)
    ctx.register_middleware("tool_execution", tool_execution)
    ctx.register_hook("on_session_reset", _drop_session)
    ctx.register_hook("on_session_finalize", _drop_session)
