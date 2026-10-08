"""bladebro browser bridge — port of dsh-browser-bladebro onto register_tool.

Registers see/act/state/vision tools over the bladebro CLI (``--json`` output),
gated on the binary's presence. Login walls are a stop policy, enforced by
prompt steering appended to error text.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

LOGIN_WALL_NOTE = (
    "Login wall encountered: do not guess credentials — stop and ask the user "
    "(the operator sets up site accounts)."
)

TOOL_SPECS = {
    "bladebro_see": {"op": "see", "desc": "Observe the current page state (text-first)."},
    "bladebro_act": {"op": "act", "desc": "Perform one action (navigate/click/fill) on the page."},
    "bladebro_state": {"op": "state", "desc": "Browser state: tabs, cookies, sessions."},
    "bladebro_vision": {"op": "vision", "desc": "Screenshot the viewport (PNG)."},
}


def register(ctx) -> None:
    binary = (ctx.get_config("binary", "") or shutil.which("bladebro") or "").strip()
    timeout = int(ctx.get_config("timeout", 120) or 120)
    # Tools stay registered (so `hermes tools` shows them); check_fn gates
    # dispatch until the binary is on PATH — same pattern as the bundled
    # spotify plugin.

    def available() -> bool:
        if not binary:
            return False
        import os
        return shutil.which(binary) is not None or os.path.exists(binary)

    def call(op: str, args: dict) -> str:
        argv = [binary, op, "--json"]
        for k, v in args.items():
            argv += [f"--{k}", str(v)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return json.dumps({"success": False, "error": f"bladebro {op} timed out after {timeout}s"})
        try:
            payload: dict = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            payload = {"raw": (proc.stdout or "")[:4000], "stderr": (proc.stderr or "")[:1000]}
        if proc.returncode != 0:
            payload.setdefault("success", False)
            # Stop policy rides along on failures that smell like auth walls.
            blob = json.dumps(payload).lower()
            if any(w in blob for w in ("login", "sign in", "log in", "password")):
                payload["steering"] = LOGIN_WALL_NOTE
            return json.dumps(payload)
        payload.setdefault("success", True)
        return json.dumps(payload)

    for tool_name, spec in TOOL_SPECS.items():
        ctx.register_tool(
            name=tool_name,
            toolset="bladebro",
            schema={
                "name": tool_name,
                "description": spec["desc"],
                "parameters": {
                    "type": "object",
                    "properties": {"args": {"type": "object", "description": "bladebro args (see bladebro --help for keys)"}},
                },
            },
            handler=(lambda op: lambda args, **kw: call(op, args.get("args") or {}))(spec["op"]),
            check_fn=available,
            description=spec["desc"],
            emoji="🌐",
        )
    logger.info("browser-bladebro: registered 4 tools via %s", binary)
