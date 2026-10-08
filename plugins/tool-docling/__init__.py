"""Docling bridge — port of dsh-tool-docling onto register_tool.

Wraps the docling CLI (never tesseract) for local document conversion:
PDF/DOCX/PPTX/XLSX/HTML/MD/CSV -> Markdown (or docling-core JSON via
read_format). Output goes to the workspace; tool returns the path + text.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

SCHEMA = {
    "name": "docling_convert",
    "description": (
        "Convert a document (PDF/DOCX/PPTX/XLSX/HTML/MD/CSV) to Markdown via local docling. "
        "Returns the converted Markdown text (truncated to max_chars, full file path always returned)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the source document"},
            "max_chars": {"type": "integer", "description": "Truncate returned text to this many chars (default 20000)", "default": 20000},
        },
        "required": ["path"],
    },
}


def register(ctx) -> None:
    binary = ctx.get_config("binary", "docling")

    def available() -> bool:
        return bool(binary) and shutil.which(binary) is not None

    def handler(path: str = "", max_chars: int = 20000, **_kwargs) -> str:
        if not path or not os.path.exists(path):
            return json.dumps({"success": False, "error": f"file not found: {path!r}"})
        if not available():
            return json.dumps({"success": False, "error": "docling CLI not available"})
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(
                [binary, path, "--to", "md", "--output", td, "--quiet"],
                capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                return json.dumps({
                    "success": False,
                    "error": (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:2000],
                })
            stem = os.path.splitext(os.path.basename(path))[0]
            out_md = os.path.join(td, f"{stem}.md")
            if not os.path.exists(out_md):
                md_candidates = [f for f in os.listdir(td) if f.endswith(".md")]
                if not md_candidates:
                    return json.dumps({"success": False, "error": "docling produced no markdown output"})
                out_md = os.path.join(td, md_candidates[0])
            text = open(out_md, encoding="utf-8", errors="replace").read()
            # Copy to durable location so the agent can read the full file.
            durable = os.path.join(tempfile.gettempdir(), "docling", f"{stem}.md")
            os.makedirs(os.path.dirname(durable), exist_ok=True)
            with open(durable, "w", encoding="utf-8") as f:
                f.write(text)
            return json.dumps({
                "success": True,
                "file": durable,
                "chars": len(text),
                "text": text[: max(0, int(max_chars))],
            })

    ctx.register_tool(
        name="docling_convert",
        toolset="docling",
        schema=SCHEMA,
        handler=lambda args, **kw: handler(path=args.get("path", ""), max_chars=args.get("max_chars", 20000)),
        check_fn=available,
        description="Convert documents to Markdown via docling.",
        emoji="📄",
    )
    logger.info("tool-docling: registered (binary=%s)", binary)
