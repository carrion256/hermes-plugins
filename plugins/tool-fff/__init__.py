"""tool-fff — native-Hermes search adapter (optional built-in override).

The original dsh-tool-fff shelled out to an invented ``fff`` CLI
(``fff grep|find … --json --max N``). That interface never existed: the
genuine fff project exposes an in-process Node API (@ff-labs/fff-node) and
an MCP server, so no Python plugin can invoke it as the old subprocess code
claimed, and no acceleration is possible from here. The subprocess path was
removed (audit repair).

This plugin now registers an *override* of the built-in ``search_files``
that delegates directly to the captured native handler — preserving the
native schema, per-task CWD/backend routing, credential guard/redaction,
pagination bounds, and error/result contract exactly. The handler is
captured from the registry before the override is installed and invoked
directly, so calls never dispatch recursively through the overridden name.

Operator opt-in required in config.yaml:
    plugins.entries.tool-fff.allow_tool_override: true
and the plugin setting:
    override_search_files: true
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    if not bool(ctx.get_config("override_search_files", False)):
        logger.info(
            "tool-fff: override_search_files disabled; built-in search_files stays active")
        return
    try:
        from tools.registry import registry

        native = registry.get_entry("search_files")
        if native is None:
            # Importing file_tools registers the built-in on import.
            import tools.file_tools  # noqa: F401
            native = registry.get_entry("search_files")
        if native is None:
            logger.warning("tool-fff: no native search_files registered; plugin stays inert")
            return

        # Direct native handler reference — never registry.dispatch (which would
        # re-enter this plugin's override). Forwards args/kwargs verbatim so
        # task_id/session_id keep per-task CWD and backend dispatch.
        ctx.register_tool(
            name="search_files",
            toolset=native.toolset,
            schema=native.schema,
            handler=native.handler,
            check_fn=native.check_fn,
            description=native.description,
            emoji=native.emoji,
            override=True,
        )
        # ctx.register_tool cannot carry max_result_size_chars; copy the native
        # cap onto the exact replacement entry (registry.get_max_result_size
        # falls back to the global default otherwise, silently changing the
        # Layer-2 result-size guard for every consumer).
        registered = registry.get_entry("search_files")
        if registered is not None and registered.handler is native.handler:
            registered.max_result_size_chars = native.max_result_size_chars
        logger.info("tool-fff: search_files override registered (native backend)")
    except Exception as exc:
        # e.g. PluginToolOverrideError without allow_tool_override — plugin stays inert,
        # built-in search_files lives on unchanged.
        logger.warning(
            "tool-fff: override refused (%s); built-in search_files stays active", exc)
