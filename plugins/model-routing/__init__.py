"""Model routing — explicit config-only role -> {model, provider} routing.

The plugin owns a small role table in its settings (``roles``), a fallback
policy (``inherit`` by default, ``refuse`` to reject unknown roles), and an
optional ``delegation_role`` naming which role materializes the delegation
pins. Resolution is deterministic: a configured role routes to exactly its
pinned model (provider omitted = the parent's provider, parentless = the
config main provider); an unknown role inherits the parent model or is
refused, per policy. Every resolution appends to a bounded in-memory audit
ring visible through ``route_status``; routes come only from the operator's
table — never inferred, measured or chosen by any selection logic.

``route_apply`` materializes the table into the host's explicit pins
(``prewalk`` target, ``delegation.model``/``provider``, the three
``auxiliary.*`` review bindings) through the native config writers, dry-run
by default, with a provenance marker in the plugin's own state; it never
touches unrelated keys.

Host seam: registers its resolver through ``ctx.register_role_resolver``
when the host provides it (one resolver per host); without the seam the
plugin still loads, its tools work, and ``route_status`` reports
``host_seam: unavailable``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque

logger = logging.getLogger(__name__)

_ROLE_NAME_RE = re.compile(r"[a-z][a-z0-9_-]*")
_ROLE_FIELDS = {"model", "provider"}
_SETTINGS_FIELDS = {"roles", "fallback", "delegation_role"}
_FALLBACKS = ("inherit", "refuse")
_AUDIT_MAXLEN = 128
_RECEIPT_KEYS = ("resolved", "role", "model", "provider", "source", "reason")
_REVIEW_SURFACES = ("curator", "background_review", "review")


def _entry_settings(ctx):
    """This plugin's raw ``plugins.entries.<id>.settings`` value via the host
    config API: a mapping, ``None`` when absent or the host API is unavailable,
    or whatever malformed value is on disk — ``_read_settings`` decides which
    is which, so a present-but-broken settings block fails loud instead of
    silently loading idle."""
    plugin_id = getattr(ctx, "plugin_id", None) or "model-routing"
    try:
        from hermes_cli.config import load_config_readonly
        entries = (load_config_readonly().get("plugins") or {}).get("entries") or {}
        entry = entries.get(plugin_id)
        settings = entry.get("settings") if isinstance(entry, dict) else None
    except Exception:  # host API unavailable — degrade to an idle plugin
        return None
    return settings


def _known_providers() -> set[str] | None:
    """Provider names the host knows (canonical catalog) or has configured
    (custom ``providers:``/``custom_providers`` entries). No network. ``None``
    when no host API is available — validation is then skipped, never faked."""
    names: set[str] = set()
    try:
        from hermes_cli.models_catalog_static import CANONICAL_PROVIDERS
        names |= {str(p.slug) for p in CANONICAL_PROVIDERS}
        names.add("custom")
    except Exception:
        pass
    try:
        from hermes_cli.config_providers import get_compatible_custom_providers
        for entry in get_compatible_custom_providers():
            for field in ("name", "provider_key"):
                value = str((entry or {}).get(field) or "").strip()
                if value:
                    names.add(value)
    except Exception:
        pass
    return names or None


def _main_provider() -> str:
    """The config main model's provider (parentless baseline); ``""`` when
    unresolvable."""
    try:
        from hermes_cli.config import load_config_readonly, split_model_config_default
        model_cfg = load_config_readonly().get("model") or {}
        _model, provider = split_model_config_default(model_cfg.get("default"))
        provider = str(provider or "").strip()
        return provider or str(model_cfg.get("provider") or "").strip()
    except Exception:
        return ""


def _read_settings(ctx) -> dict:
    """Validate the settings mapping; raise ``ValueError`` naming the offending
    key on any misconfiguration (fail-loud at registration)."""
    raw = _entry_settings(ctx)
    if raw is None:
        raw = {}
    elif not isinstance(raw, dict):
        raise ValueError(
            "model-routing: 'settings' must be a mapping of setting name -> value, "
            f"got {type(raw).__name__}"
        )
    unknown = set(raw) - _SETTINGS_FIELDS
    if unknown:
        raise ValueError(
            f"model-routing: unknown settings key(s) {sorted(unknown)}; "
            f"allowed: {sorted(_SETTINGS_FIELDS)}"
        )
    fallback = raw.get("fallback", "inherit")
    if fallback not in _FALLBACKS:
        raise ValueError(
            f"model-routing: 'fallback' must be one of {list(_FALLBACKS)}, got {fallback!r}"
        )
    roles_raw = raw.get("roles", {})
    if not isinstance(roles_raw, dict):
        raise ValueError("model-routing: 'roles' must be a mapping of role name -> spec")
    roles: dict[str, dict[str, str | None]] = {}
    for name, body in roles_raw.items():
        if not isinstance(name, str) or not _ROLE_NAME_RE.fullmatch(name):
            raise ValueError(
                f"model-routing: role name {name!r} must match [a-z][a-z0-9_-]*"
            )
        if not isinstance(body, dict):
            raise ValueError(f"model-routing: role {name!r} must be a mapping")
        bad = set(body) - _ROLE_FIELDS
        if bad:
            raise ValueError(
                f"model-routing: role {name!r} has unknown field(s) {sorted(bad)}; "
                f"allowed: {sorted(_ROLE_FIELDS)}"
            )
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(
                f"model-routing: role {name!r} needs a non-empty 'model' string"
            )
        provider = body.get("provider")
        if provider is not None and (not isinstance(provider, str) or not provider.strip()):
            raise ValueError(
                f"model-routing: role {name!r} 'provider' must be a non-empty string or omitted"
            )
        roles[name] = {"model": model.strip(),
                       "provider": provider.strip() if provider else None}

    delegation_role = raw.get("delegation_role")
    if delegation_role is not None:
        if not isinstance(delegation_role, str):
            raise ValueError("model-routing: 'delegation_role' must be a role name string")
        delegation_role = delegation_role.strip()
        if delegation_role and delegation_role not in roles:
            raise ValueError(
                f"model-routing: 'delegation_role' {delegation_role!r} names no configured role"
            )
        delegation_role = delegation_role or None

    providers = _known_providers()
    provider_validation = "checked" if providers is not None else "skipped"
    if providers is not None:
        for name, spec in roles.items():
            if spec["provider"] and spec["provider"] not in providers:
                raise ValueError(
                    f"model-routing: role {name!r} provider {spec['provider']!r} is neither a "
                    "known nor a configured provider"
                )
    return {"roles": roles, "fallback": fallback,
            "delegation_role": delegation_role, "provider_validation": provider_validation}


def _make_resolver(roles: dict, fallback: str, audit: deque):
    """Build the pure, deterministic resolver the host registers. Every call
    appends ``{receipt, session_id, time}`` to the bounded audit ring."""

    def resolve_role_model(role, *, session_id: str = "", parent_model: str = "",
                           parent_provider: str = "") -> dict:
        role = str(role or "")
        spec = roles.get(role)
        if spec is not None:
            provider = spec["provider"] or str(parent_provider or "").strip() or _main_provider()
            receipt = {"resolved": True, "role": role, "model": spec["model"],
                       "provider": provider, "source": "route",
                       "reason": f"role '{role}' routed to {spec['model']}"}
        elif fallback == "refuse":
            receipt = {"resolved": False, "role": role, "model": None, "provider": None,
                       "source": "refused",
                       "reason": f"unknown role '{role}' and fallback is 'refuse'"}
        else:
            receipt = {"resolved": False, "role": role,
                       "model": str(parent_model or "") or None,
                       "provider": str(parent_provider or "") or None,
                       "source": "inherit",
                       "reason": f"unknown role '{role}'; inheriting the parent model"}
        audit.append({"receipt": dict(receipt), "session_id": str(session_id or ""),
                      "time": time.time()})
        return dict(receipt)

    return resolve_role_model


def _target_pins(roles: dict, delegation_role: str | None) -> list:
    """The explicit pins ``route_apply`` materializes: ``(kind, key, value)``
    with ``kind`` in {"config", "plugin:prewalk"}; provider pins only when the
    role names a provider."""
    pins: list = []
    executor = roles.get("executor")
    if executor:
        pins.append(("plugin:prewalk", "prewalk.target_model", executor["model"]))
        if executor["provider"]:
            pins.append(("plugin:prewalk", "prewalk.target_provider", executor["provider"]))
    if delegation_role:
        spec = roles[delegation_role]
        pins.append(("config", "delegation.model", spec["model"]))
        if spec["provider"]:
            pins.append(("config", "delegation.provider", spec["provider"]))
    reviewer = roles.get("reviewer")
    if reviewer:
        for surface in _REVIEW_SURFACES:
            pins.append(("config", f"auxiliary.{surface}.model", reviewer["model"]))
            if reviewer["provider"]:
                pins.append(("config", f"auxiliary.{surface}.provider", reviewer["provider"]))
    return pins


def _walk(mapping: dict, dotted: str):
    """Value at a dotted path, ``None`` when any hop is missing or scalar."""
    node = mapping
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_walk(mapping: dict, dotted: str, value) -> None:
    """Set one dotted path, creating intermediate mappings on demand."""
    parts = dotted.split(".")
    node = mapping
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _current_values(pins: list) -> dict:
    """Current value of every pin key, read from the raw user config."""
    from hermes_cli.config import read_user_config_raw
    raw = read_user_config_raw()
    current = {}
    for kind, key, _value in pins:
        if kind == "plugin:prewalk":
            entry_key = key.split(".", 1)[1]
            current[key] = _walk(raw, f"plugins.entries.prewalk.settings.{entry_key}")
        else:
            current[key] = _walk(raw, key)
    return current


def register(ctx) -> None:
    settings = _read_settings(ctx)
    roles = settings["roles"]
    fallback = settings["fallback"]
    delegation_role = settings["delegation_role"]
    provider_validation = settings["provider_validation"]

    audit: deque = deque(maxlen=_AUDIT_MAXLEN)
    resolver = _make_resolver(roles, fallback, audit)

    host_seam = "unavailable"
    seam = getattr(ctx, "register_role_resolver", None)
    if callable(seam):
        seam(resolver)
        host_seam = "registered"

    def route_status(args=None, **_kw):
        config = {"fallback": fallback,
                  "roles": {name: {"model": spec["model"], "provider": spec["provider"]}
                            for name, spec in sorted(roles.items())}}
        if delegation_role:
            config["delegation_role"] = delegation_role
        effective = {name: {"model": spec["model"],
                            "provider": spec["provider"] or "parent"}
                     for name, spec in sorted(roles.items())}
        return json.dumps({
            "config": config, "effective": effective,
            "audit": list(audit), "host_seam": host_seam,
            "provider_validation": provider_validation,
        }, sort_keys=True)

    def route_apply(args=None, **_kw):
        args = args or {}
        apply = bool(args.get("apply", False))
        pins = _target_pins(roles, delegation_role)
        try:
            current = _current_values(pins)
        except Exception as exc:
            return json.dumps({"applied": False, "dry_run": not apply, "diff": {},
                               "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True)
        diff = {key: {"old": current[key], "new": value}
                for _kind, key, value in pins if current[key] != value}
        receipt = {"applied": False, "dry_run": not apply, "diff": diff, "written": []}
        if not apply:
            return json.dumps(receipt, sort_keys=True)

        written: list = []
        try:
            from hermes_cli.config import (atomic_config_write, get_config_path,
                                           read_user_config_raw)
            if diff:
                raw = read_user_config_raw()
                for kind, key, _v in pins:
                    if key in diff:
                        path = key if kind == "config" else (
                            f"plugins.entries.prewalk.settings.{key.split('.', 1)[1]}")
                        _set_walk(raw, path, diff[key]["new"])
                atomic_config_write(get_config_path(), raw)
                written = [key for _kind, key, _v in pins if key in diff]
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"
        receipt["applied"] = "error" not in receipt
        receipt["written"] = sorted(written)
        if receipt["applied"] and written:
            state = getattr(ctx, "state", None)
            if state is None:
                logger.warning("model-routing: provenance marker unavailable")
            else:
                marker = {"plugin": "model-routing",
                          "roles": {name: dict(spec) for name, spec in sorted(roles.items())},
                          "keys": sorted(key for _k, key, _v in pins),
                          "applied_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                try:
                    state.set("route_apply", marker)
                except Exception:
                    logger.warning("model-routing: provenance marker not persisted", exc_info=True)
                else:
                    receipt["provenance"] = marker
        return json.dumps(receipt, sort_keys=True)

    ctx.register_tool(
        name="route_status", toolset="model-routing",
        schema={"name": "route_status",
                "description": "Report the model-routing table: validated config roles, "
                "effective per-role model/provider, this plugin's bounded resolution audit "
                "ring, the host resolver seam state, and whether provider validation ran.",
                "parameters": {"type": "object", "properties": {}}},
        handler=route_status,
        description="Inspect the model-routing table, effective routes and audit ring.",
    )
    ctx.register_tool(
        name="route_apply", toolset="model-routing",
        schema={"name": "route_apply",
                "description": "Materialize the routing table into explicit host pins "
                "(prewalk target, delegation, the three auxiliary review bindings) through "
                "the native config writers. Dry-run by default; pass {\"apply\": true} to "
                "write. Returns the exact pin diff; unrelated keys are never touched.",
                "parameters": {"type": "object",
                               "properties": {"apply": {
                                   "type": "boolean",
                                   "description": "Write the pins (default false: dry-run "
                                   "reporting only)."}}}},
        handler=route_apply,
        description="Dry-run (default) or apply the routing table as explicit host pins.",
    )
    if not roles:
        logger.info("model-routing: no roles configured; resolver registered idle "
                    "(host_seam=%s)", host_seam)
    else:
        logger.info("model-routing: %d role(s), fallback=%s, host_seam=%s",
                    len(roles), fallback, host_seam)
