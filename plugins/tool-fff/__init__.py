"""fff search override — port of dsh-tool-fff onto register_tool(override=True).

Registers an override of the built-in search_files tool. When an fff binary is
configured/available, search runs through it; otherwise the handler fails open
to ripgrep (or returns a clear error if that's missing too) so search never
goes dark. The built-in remains reachable by disabling this plugin.

Operator opt-in required in config.yaml:
    plugins.entries.tool-fff.allow_tool_override: true
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

SCHEMA = {
    "name": "search_files",
    "description": "Search file contents (regex) or find files by name glob. Prefer fff when configured; ripgrep fallback.",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex (content) or glob (files)"},
            "target": {"type": "string", "enum": ["content", "files"], "default": "content"},
            "path": {"type": "string", "default": "."},
            "limit": {"type": "integer", "default": 50},
        },
        "required": ["pattern"],
    },
}


def register(ctx) -> None:
    configured = (ctx.get_config("binary", "") or "").strip()
    fff = configured or (shutil.which("fff") or shutil.which("fffind"))

    def handler(pattern: str = "", target: str = "content", path: str = ".", limit: int = 50, **_kw) -> str:
        if not pattern:
            return json.dumps({"success": False, "error": "pattern required"})
        if fff and os.path.exists(fff):
            try:
                proc = subprocess.run(
                    [fff, "grep" if target == "content" else "find", pattern, path,
                     "--json" if target == "content" else "--files", "--max", str(limit)],
                    capture_output=True, text=True, timeout=60,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return json.dumps({"success": True, "backend": "fff",
                                       "output": proc.stdout[:60000]})
            except (subprocess.TimeoutExpired, OSError):
                pass  # fail open to rg below
        rg = shutil.which("rg")
        if not rg:
            return json.dumps({"success": False, "error": "neither fff nor ripgrep available"})
        argv = [rg, "--line-number", "--color", "never", "--max-count", str(limit)]
        if target == "files":
            argv += ["--files", "--glob", pattern]
        else:
            argv += ["--regexp", pattern]
        argv.append(path)
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        out = (proc.stdout or "").strip()
        return json.dumps({"success": True, "backend": "ripgrep", "output": out[:60000]})

    try:
        primary = "search_files" if bool(ctx.get_config("override_search_files", False)) else "fff_search"
        ctx.register_tool(
            name=primary,
            toolset="fff_search",
            schema=dict(SCHEMA, name=primary),
            handler=lambda args, **kw: handler(**{k: v for k, v in args.items() if k in
                                                  ("pattern", "target", "path", "limit")}),
            override=primary == "search_files",
            description="fff/ripgrep-backed search (tool-fff).",
        )
        logger.info("tool-fff: %s registered (fff=%s)", primary, fff or "absent, rg fallback")
    except Exception as exc:
        # e.g. PluginToolOverrideError without allow_tool_override — plugin stays inert, built-in lives.
        logger.warning("tool-fff: override refused (%s); built-in search_files stays active", exc)
