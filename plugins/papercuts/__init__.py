"""Papercuts bridge — port of dsh-papercuts onto register_tool.

Registers a papercuts tool that forwards a single mode string
(``report``/``list``/``ack``/``resolve``/``stats``) plus optional text to the
native papercuts binary. No DB logic here: the binary owns the store.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

_TOOLS = ("report", "list", "ack", "resolve", "stats")

SCHEMA = {
    "name": "papercuts",
    "description": (
        "File and triage tool grievances. mode=report needs --category, --severity, --summary; "
        "mode=list shows open items; ack/resolve take a papercut id; stats summarizes. "
        "text carries extra flags as a shell-quoted-free single string, e.g. "
        "'--category tooling --severity P2 --summary \"what happened\"'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": list(_TOOLS), "description": "papercuts subcommand"},
            "text": {"type": "string", "description": "Trailing flags/args, space-separated (optional)"},
        },
        "required": ["mode"],
    },
}


def register(ctx) -> None:
    binary = ctx.get_config("binary", "/home/common/projects/papercuts/papercuts")

    def available() -> bool:
        import os
        return bool(binary) and (shutil.which(binary) is not None or os.path.exists(binary))

    def handler(mode: str = "list", text: str = "", **_kwargs) -> str:
        if mode not in _TOOLS:
            return json.dumps({"success": False, "error": f"unknown mode {mode!r}"})
        argv = [binary, mode] + ([a for a in text.split() if a] if text else [])
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return json.dumps({"success": False, "error": "papercuts timed out"})
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return json.dumps({"success": False, "error": err or f"exit {proc.returncode}", "stdout": out})
        return json.dumps({"success": True, "mode": mode, "output": out})

    ctx.register_tool(
        name="papercuts",
        toolset="papercuts",
        schema=SCHEMA,
        handler=lambda args, **kw: handler(mode=args.get("mode", "list"), text=args.get("text", "")),
        check_fn=available,
        description="File tool grievances (papercuts).",
    )
    ctx.register_cli_command(
        name="papercuts",
        help="File or triage tool grievances (papercuts).",
        setup_fn=lambda sub: sub.add_argument("rest", nargs="*", help="papercuts args, e.g. report --category tooling --severity P2 --summary x"),
        handler_fn=lambda args: subprocess.run([binary, *(args.rest or ["list"])]).returncode,
    )
    logger.info("papercuts: bridge registered -> %s", binary)
