"""Docling conversion over the qualified native backend.

Backend: Python Docling (pip) CLI. Local files only — the plugin resolves
sources against the task cwd, refuses remote sources/unsupported terminal
backends, and guards sensitive paths with the host's own read-side guard.

Cache identity is the source CONTENT hash (sha256) plus converter version
plus converter executable identity (sha256) plus options, including the input
format suffix — never the origin binary FNV hash or filename stem. Source and
converter identity are re-sampled at
the conversion boundary and RECHECKED after execution; publication is refused
on identity drift. Publication is serialized per cache key through the host's
cross-process state transaction lock, staged into unique temp files and
committed with an atomically replaced marker; conversion failure never
overwrites a valid cache entry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Qualified backend: docling convert <source> --to md --to json ...
SUPPORTED_CONVERSION = "docling convert <source> --to md --to json --image-export-mode placeholder --output <dir> --quiet"
PAGE_RANGE_RE = re.compile(r"^\d+(-\d+)?$")
MAX_CHARS_CAP = 100_000
MAX_SECTIONS = 10
MAX_SELECT_CHARS = 50_000


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg})


def _task_cwd(task_id) -> str | None:
    """Recorded cwd for THIS task only. No ambient fallback: untrusted/absent
    task identity means no conversion (the caller must fix the plumbing)."""
    if not task_id:
        return None
    try:
        from tools.terminal_tool import get_session_cwd
        return get_session_cwd(str(task_id))
    except Exception:
        return None


def _terminal_backend(task_id) -> str:
    from tools.file_tools_paths import _terminal_env_type_for_task
    return _terminal_env_type_for_task(str(task_id or "default"))


def _source_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_key(src_hash: str, ident: str, version: str, options: dict) -> str:
    """Cache identity binds the ACTUAL converter executable (sha256) and version
    alongside the source content hash — never a process-cached version string."""
    return hashlib.sha256(json.dumps(
        {"src": src_hash, "version": version, "converter_sha256": ident,
         "options": options}, sort_keys=True).encode()).hexdigest()[:20]



def _item_anchors(items) -> list:
    """Real Docling JSON provenance lives on each item: item.prov[] carries
    page_no/charspan/bbox; self_ref/parent are the item's own refs and are
    preserved even when the item carries no prov entry (HTML case)."""
    anchors = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        provs = [p for p in (item.get("prov") or []) if isinstance(p, dict)]
        base = {"self_ref": item.get("self_ref") or item.get("self"),
                "parent": item.get("parent")}
        if provs:
            anchors.extend({**base, "page_no": p.get("page_no"),
                            "charspan": p.get("charspan"), "bbox": p.get("bbox")}
                           for p in provs)
        elif base["self_ref"] is not None:
            anchors.append({**base, "page_no": None, "charspan": None, "bbox": None})
    return anchors


def _extract_anchors(document: dict) -> dict:
    """Parse the qualified Docling JSON schema: per-item self_ref/parent with
    nested prov page_no/charspan/bbox. Missing provenance stays explicit."""
    if not isinstance(document, dict):
        return {"available": False,
                "reason": "converter output is not a JSON object",
                "anchors": [], "pages": [],
                "page_provenance": "missing (explicit)"}
    anchors = (_item_anchors(document.get("texts"))
               + _item_anchors(document.get("tables"))
               + _item_anchors(document.get("pictures")))
    page_nos = sorted({a["page_no"] for a in anchors if a.get("page_no") is not None})
    reason = "" if anchors else (
        "converter emitted no texts/tables/pictures items with self_ref "
        "(page provenance also unavailable)")
    return {
        "available": bool(anchors),
        "reason": reason,
        "anchors": anchors[:200],
        "pages": page_nos,
        "page_provenance": "present" if page_nos else "missing (explicit)",
    }


def _select_sections(markdown: str, queries: list) -> list:
    """Bounded heading-based section selection; references/uncertainty preserved
    by returning ORIGINAL markdown slices verbatim."""
    blocks, current, current_title = [], [], None
    for line in markdown.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            blocks.append((current_title, "\n".join(current)))
            current_title, current = m.group(2).strip(), [line]
        else:
            current.append(line)
    blocks.append((current_title, "\n".join(current)))
    out = []
    for query in queries[:MAX_SECTIONS]:
        q = str(query).strip().lower()
        match = next((text for title, text in blocks if title and q in title.lower()), None)
        if match is not None:
            out.append({"query": query, "text": match[:MAX_SELECT_CHARS]})
    return out


def register(ctx) -> None:
    binary = ctx.get_config("binary", "docling")
    state = ctx.state

    def _converter_identity() -> tuple[str | None, str | None]:
        """Sample launcher sha256 and the full bounded package version report.
        The launcher hash alone does not identify installed dependencies; the reported
        versions bind the actual package change (not every transitive dependency)."""
        executable = shutil.which(binary) or (binary if binary and os.path.exists(binary) else None)
        if executable is None:
            return None, None
        try:
            digest = hashlib.sha256()
            with open(os.path.realpath(executable), "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            executable_hash = digest.hexdigest()
        except OSError:
            return None, None
        version = None
        for probe in ([binary, "convert", "--version"], [binary, "--version"]):
            try:
                proc = subprocess.run(probe, capture_output=True, text=True, timeout=60)
                report = proc.stdout or ""
                if (proc.returncode == 0 and 0 < len(report) <= 8192
                        and re.search(r"(?im)^docling version:?\s+\d+\.\d+\.\d+\S*[ \t]*$",
                                      report)):
                    version = report.strip()
                    break
            except (OSError, subprocess.TimeoutExpired, UnicodeError):
                continue
        return executable_hash, version

    def available() -> bool:
        executable_hash, version = _converter_identity()
        return bool(binary) and executable_hash is not None and version is not None

    def _convert(args: dict, *, task_id=None, session_id=None, **_kw) -> str:
        src = str(args.get("path", "")).strip()
        if not src:
            return _err("path is required")
        if "://" in src or src.startswith(("file:", "s3:", "gs:", "http", "https:", "sftp:")):
            return _err("remote sources are unsupported: docling runs on local files only "
                        f"({SUPPORTED_CONVERSION})")
        try:
            backend = _terminal_backend(task_id)
        except Exception:
            return _err("native task backend resolver unavailable; refusing local conversion")
        if backend not in ("local", "host"):
            return _err(f"terminal backend '{backend}' is unsupported: local document "
                        "conversion needs local file access; no remote upload is performed")
        cwd = _task_cwd(task_id)
        if not cwd:
            return _err("no task-cwd recorded for this task_id; refusing to guess a working directory")
        path = src if os.path.isabs(src) else os.path.join(cwd, src)
        path = os.path.realpath(path)
        if not os.path.isfile(path):
            return _err(f"file not found: {src!r} (resolved: {path})")
        try:
            from agent.file_safety import get_read_block_error
        except Exception:
            return _err("canonical native read guard unavailable; refusing conversion "
                        "rather than drifting to a weaker copied guard")
        blocked = get_read_block_error(path)
        if blocked:
            return _err(f"sensitive file refused: {src!r}: {blocked}")
        raw_max = args.get("max_chars")
        if raw_max is None:
            max_chars = 20000
        elif isinstance(raw_max, bool) or not isinstance(raw_max, int):
            return _err(f"max_chars must be an integer, got {raw_max!r}")
        else:
            max_chars = max(0, min(raw_max, MAX_CHARS_CAP))
        page_range = str(args.get("page_range") or "").strip()
        if page_range and not PAGE_RANGE_RE.match(page_range):
            return _err(f"invalid page_range {page_range!r}: expected 'N' or 'N-M'")
        # Docling falls back to the filename's format when content is ambiguous.
        options = {"to": ["md", "json"], "image_export_mode": "placeholder",
                   "input_suffix": os.path.splitext(path)[1].lower(),
                   "page_range": page_range or None}
        try:
            src_hash = _source_hash(path)
        except OSError as exc:
            return _err(f"cannot read source: {exc}")
        ident, version = _converter_identity()
        if ident is None or version is None:
            return _err("docling version probe failed; backend availability is unverified")
        cache_key = _cache_key(src_hash, ident, version, options)
        stem = os.path.splitext(os.path.basename(path))[0]
        cache_dir = os.path.join(state.data_dir, "artifacts", cache_key)
        marker = os.path.join(cache_dir, "artifact.json")

        def _load_valid() -> tuple[str, dict, str, str] | None:
            """A cache entry is valid only when the marker matches the live-sampled
            identities (source hash, converter version AND converter executable
            sha256), the pair files load cleanly (the marker itself is never
            accepted as the converted document), and the source STILL hashes to
            the marker's bytes (post-load recheck). Returns the ADOPTED marker
            filenames so callers always reference files that exist."""
            try:
                with open(marker, encoding="utf-8") as fh:
                    meta = json.load(fh)
                if (not isinstance(meta, dict)
                        or meta.get("source_hash") != src_hash
                        or meta.get("converter_version") != version
                        or meta.get("converter_sha256") != ident
                        or meta.get("options") != options):
                    return None
                md_name, json_name = meta.get("md"), meta.get("json")
                if (not isinstance(md_name, str) or not isinstance(json_name, str)
                        or md_name == os.path.basename(marker)
                        or json_name == os.path.basename(marker)):
                    return None
                with open(os.path.join(cache_dir, md_name), encoding="utf-8") as fh:
                    md_text = fh.read()
                with open(os.path.join(cache_dir, json_name), encoding="utf-8") as fh:
                    document = json.load(fh)
                if _source_hash(path) != src_hash:
                    return None
            except (OSError, ValueError, KeyError):
                return None
            return md_text, document, md_name, json_name

        cached = _load_valid()
        if cached is None:
            # The conversion boundary: re-sample source and converter identities so
            # the cache key and the post-run recheck describe exactly the bytes and
            # binary the subprocess will see.
            try:
                src_hash = _source_hash(path)
            except OSError as exc:
                return _err(f"cannot read source: {exc}")
            ident, version = _converter_identity()
            if ident is None or version is None:
                return _err("docling version probe failed; backend availability is unverified")
            cache_key = _cache_key(src_hash, ident, version, options)
            cache_dir = os.path.join(state.data_dir, "artifacts", cache_key)
            marker = os.path.join(cache_dir, "artifact.json")
            argv_base = [binary, "convert", path, "--to", "md", "--to", "json",
                         "--image-export-mode", "placeholder"]
            if page_range:
                argv_base += ["--page-range", page_range]
            with tempfile.TemporaryDirectory() as workdir:
                # Qualified conversion: --output <dir> --quiet.
                argv = argv_base + ["--output", workdir, "--quiet"]
                try:
                    proc = subprocess.run(argv, capture_output=True, text=True,
                                          timeout=600, cwd=cwd)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    return _err(f"docling invocation failed: {exc}")
                if proc.returncode != 0:
                    # Failed conversion: nothing is published; a previous valid cache
                    # entry (if any) stays untouched, and none is created.
                    return _err("docling conversion failed: "
                                + (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:2000])
                md_out = os.path.join(workdir, f"{stem}.md")
                json_out = os.path.join(workdir, f"{stem}.json")
                if not (os.path.exists(md_out) and os.path.exists(json_out)):
                    produced = sorted(os.listdir(workdir))
                    return _err(f"docling produced no md/json pair for {stem} (output dir holds {produced})")
                try:
                    with open(json_out, encoding="utf-8") as fh:
                        document = json.load(fh)
                except ValueError as exc:
                    return _err(f"docling produced invalid JSON: {exc}")
                with open(md_out, encoding="utf-8") as fh:
                    md_text = fh.read()
                # Post-conversion identity recheck (CWE-367): if the source bytes or
                # the converter changed while conversion ran, the outputs describe an
                # identity we cannot attest — refuse publication, touch nothing.
                post_src_hash = _source_hash(path)
                post_ident, post_version = _converter_identity()
                if post_src_hash != src_hash or post_ident != ident or post_version != version:
                    return _err("source or converter identity changed during conversion; "
                                "refusing to publish artifacts under the sampled identity (retry)")
                try:
                    from hermes_cli.plugins_state import _locked_plugin_state
                except Exception:
                    return _err("native plugin-state transaction lock unavailable; refusing "
                                "unguarded artifact publication rather than drifting to a "
                                "weaker copied guard")
                with _locked_plugin_state(Path(marker)):
                    inside = _load_valid()
                    if inside is None:
                        # Per-file atomic publication with UNIQUE staging and
                        # marker-last: concurrent same-key writers can neither share
                        # temp names nor half-publish; an existing valid entry is
                        # never overwritten.
                        os.makedirs(cache_dir, exist_ok=True)
                        # Fixed cache-owned names, DISTINCT from the marker: the
                        # converted pair can never be overwritten by, or collide
                        # with, artifact.json metadata, whatever the source basename.
                        md_file, json_file = "document.md", "document.json"
                        for src_out, name in ((md_out, md_file), (json_out, json_file)):
                            fd, staged = tempfile.mkstemp(dir=cache_dir, prefix=f".{name}.", suffix=".part")
                            os.close(fd)
                            shutil.copyfile(src_out, staged)
                            os.replace(staged, os.path.join(cache_dir, name))
                        fd, marker_tmp = tempfile.mkstemp(dir=cache_dir, prefix=".artifact.json.", suffix=".part")
                        with os.fdopen(fd, "w", encoding="utf-8") as fh:
                            json.dump({"md": md_file, "json": json_file, "source_hash": src_hash,
                                       "converter_version": version, "converter_sha256": ident,
                                       "options": options}, fh)
                        os.replace(marker_tmp, marker)
                        from_cache = False
                    else:
                        # A concurrent writer published this exact identity while we
                        # waited on the lock: adopt theirs, publish nothing.
                        md_text, document, md_file, json_file = inside
                        from_cache = True
        else:
            md_text, document, md_file, json_file = cached
            from_cache = True

        provenance = _extract_anchors(document)
        result = {
            "success": True,
            "source": src,
            "source_hash": src_hash,
            "converter_version": version,
            "converter_sha256": ident,
            "page_range": page_range or None,
            "markdown_path": os.path.join(cache_dir, md_file),
            "json_path": os.path.join(cache_dir, json_file),
            "artifact_id": cache_key,
            "from_cache": from_cache,
            "provenance": provenance,
            "chars": len(md_text),
            "text": md_text[:max_chars],
        }
        sections = args.get("sections")
        if isinstance(sections, list) and sections:
            result["selected_sections"] = _select_sections(md_text, sections)
            if provenance["page_provenance"].startswith("missing"):
                result["selected_sections_uncertainty"] = (
                    "page-level provenance unavailable (explicit); section/table "
                    "self_ref+parent anchors retained from native JSON, section "
                    "text verbatim from the converted markdown")
        return json.dumps(result)

    ctx.register_tool(
        name="docling_convert",
        toolset="docling",
        schema={
            "name": "docling_convert",
            "description": (
                "Convert a LOCAL document (PDF/DOCX/PPTX/XLSX/HTML/MD/CSV) to Markdown + native "
                "Docling JSON via the qualified backend: docling convert <source> --to md --to json "
                "--image-export-mode placeholder. Task-cwd-aware; remote sources and non-local "
                "terminal backends are refused. Results are cached per content sha256 + converter "
                "version + options under the plugin's own artifacts dir. "
                "Qualified backend version reported per call (converter_version); "
                "supported backend: Python Docling (pip)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Local source document path (relative paths resolve against the task cwd)"},
                    "max_chars": {"type": "integer", "minimum": 0, "maximum": 100000,
                                  "description": "Truncate returned markdown text (0-100000, default 20000; enforced at call time)"},
                    "page_range": {"type": "string",
                                   "description": "Bounded page window, native --page-range, e.g. '1-4'"},
                    "sections": {"type": "array", "items": {"type": "string"},
                                 "description": "Bounded heading-anchor selection (<=10), verbatim markdown slices"},
                },
                "required": ["path"],
            },
        },
        handler=lambda args, **kw: _convert(args or {}, **kw),
        check_fn=available,
        description="Convert local documents to Markdown/JSON via the qualified docling backend.",
        emoji="📄",
    )
    logger.info("tool-docling: registered (binary=%s, qualified conversion=%s)",
                binary, SUPPORTED_CONVERSION)
