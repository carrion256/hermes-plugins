"""Behavioral checks for tool-docling path guards, cache identity and provenance.

The conversion backend is exercised through a fake docling executable that
records its argv; the parent runs the real-docling smoke separately.
"""
import hashlib
import json
import os
import stat
import pytest

FAKE = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
log = os.environ.get("FAKE_DOCLING_LOG")
if "--version" in argv:
    if log:
        open(log, "a").write(json.dumps({"argv": argv}) + "\\n")
    _vf = os.environ.get("FAKE_DOCLING_VERSION_FILE")
    if _vf:
        exec(compile(open(_vf, encoding="utf-8").read(), "<version>", "exec"), globals())
        sys.exit(0)
    if os.environ.get("FAKE_DOCLING_VERSION_FAIL"):
        print("Platform: Linux-test")
        sys.exit(0 if os.environ.get("FAKE_DOCLING_VERSION_FAIL") == "footer" else 1)
    print("Docling version: 2.137.0")
    print("Docling Core version: 2.101.1")
    print("Docling IBM Models version: 4.0.3")
    print("Docling Parse version: 7.22.2")
    print("Python: cpython-312 (3.12.13)")
    print("Platform: Linux-test")
    sys.exit(0)
if log:
    open(log, "a").write(json.dumps({"argv": argv}) + "\\n")
src = argv[1] if argv[0] == "convert" else argv[0]
output = argv[argv.index("--output") + 1]
source = open(src, encoding="utf-8", errors="replace").read()
if "FAIL" in source:
    print("conversion exploded", file=sys.stderr)
    sys.exit(1)
stem = os.path.splitext(os.path.basename(src))[0]
md = "# Title\\n\\nBody %s\\n\\n# Sec A\\n\\nalpha\\n\\n# Sec B\\n\\nbeta\\n" % source
open(os.path.join(output, stem + ".md"), "w").write(md)
# Real Docling JSON shape: provenance nested per item, page_no/charspan/bbox.
# NOPROV: items keep self_ref/parent but carry no prov (HTML).
# NOANCHORS: no anchored items at all.
if "NOANCHORS" in source:
    items = []
elif "NOPROV" in source:
    items = [{"self_ref": "section_1", "parent": "_root_"},
             {"self_ref": "table_1", "parent": "section_1"}]
else:
    items = [{"self_ref": "text_1", "parent": "_root_",
              "prov": [{"page_no": 1, "charspan": [0, 10], "bbox": {"l": 0, "t": 0}}]}]
open(os.path.join(output, stem + ".json"), "w").write(
    json.dumps({"texts": items, "tables": [], "pictures": []}))
"""


@pytest.fixture
def docling(load_plugin, tmp_path, monkeypatch):
    from tools.terminal_tool import record_session_cwd
    fake = tmp_path / "docling"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_DOCLING_LOG", str(log))
    record_session_cwd("task-t", str(tmp_path))
    ctx = load_plugin("tool-docling", settings={"binary": str(fake)})
    return ctx, log, tmp_path


def _convert(ctx, args):
    return json.loads(ctx.tool("docling_convert", args, task_id="task-t"))


def _calls(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_remote_source_refused(docling):
    ctx, _, _ = docling
    out = _convert(ctx, {"path": "https://example.com/a.pdf"})
    assert out["success"] is False and "remote sources are unsupported" in out["error"]


def test_missing_task_id_refused(docling):
    ctx, _, tmp = docling
    (tmp / "x.md").write_text("hello")
    out = json.loads(ctx.tool("docling_convert", {"path": "x.md"}))
    assert out["success"] is False and "no task-cwd" in out["error"]


def test_non_local_backend_refused(docling, monkeypatch):
    ctx, log, tmp = docling
    (tmp / "a.md").write_text("hello")
    import tools.file_tools_paths as ftp
    monkeypatch.setattr(ftp, "_terminal_env_type_for_task",
                        lambda task_id="default": "docker")
    out = _convert(ctx, {"path": "a.md"})
    assert out["success"] is False and "backend 'docker'" in out["error"]
    # Refusal happens before any host path read or CLI invocation.
    assert _calls(log) == []


def test_relative_path_resolves_against_task_cwd(docling):
    ctx, _, tmp = docling
    (tmp / "note.html").write_text("hello")
    out = _convert(ctx, {"path": "note.html"})
    assert out["success"], out.get("error")
    report = out["converter_version"]
    assert "Docling version: 2.137.0" in report and report.count("\n") >= 5
    assert report.splitlines()[-1].startswith("Platform:")


def test_sensitive_file_guard(docling):
    ctx, _, tmp = docling
    (tmp / ".env").write_text("SECRET=1")
    out = _convert(ctx, {"path": ".env"})
    assert out["success"] is False and "sensitive file refused" in out["error"]


def test_canonical_guard_blocks_skill_hub(docling):
    ctx, _, tmp = docling
    # The canonical read guard (not the dashboard subset) denies internal
    # skill-hub caches even for converter-supported markdown.
    hub = tmp / "hermes-home" / "skills" / ".hub"
    hub.mkdir(parents=True)
    (hub / "payload.md").write_text("injected cache")
    out = _convert(ctx, {"path": str(hub / "payload.md")})
    assert out["success"] is False
    assert "sensitive file refused" in out["error"]
    assert "prompt injection" in out["error"]


def test_same_basename_different_content_get_separate_cache(docling):
    ctx, _, tmp = docling
    (tmp / "x").mkdir(); (tmp / "y").mkdir()
    (tmp / "x" / "doc.md").write_text("content-one")
    (tmp / "y" / "doc.md").write_text("content-two")
    a = _convert(ctx, {"path": str(tmp / "x" / "doc.md")})
    b = _convert(ctx, {"path": str(tmp / "y" / "doc.md")})
    assert a["success"] and b["success"]
    assert a["artifact_id"] != b["artifact_id"]
    assert a["markdown_path"] != b["markdown_path"]


def test_cache_hit_and_invalidation(docling):
    ctx, log, tmp = docling
    src = tmp / "c.md"
    src.write_text("one")
    first = _convert(ctx, {"path": str(src)})
    second = _convert(ctx, {"path": str(src)})
    assert first["from_cache"] is False
    assert second["from_cache"] is True
    conversions = [c for c in _calls(log) if "--version" not in c["argv"]]
    assert len(conversions) == 1
    src.write_text("two")  # content drift invalidates by hash, not mtime
    third = _convert(ctx, {"path": str(src)})
    assert third["from_cache"] is False
    assert third["source_hash"] != first["source_hash"]


def test_failed_conversion_publishes_no_stale_cache(docling):
    ctx, log, tmp = docling
    src = tmp / "bad.md"
    src.write_text("FAIL here")
    out = _convert(ctx, {"path": str(src)})
    assert out["success"] is False and "conversion exploded" in out["error"]
    assert not any((ctx.context.state.data_dir / "artifacts").glob("*/artifact.json"))
    src.write_text("now fine")
    ok = _convert(ctx, {"path": str(src)})
    assert ok["success"] and ok["from_cache"] is False


def test_page_range_forwarded_and_validated(docling):
    ctx, log, tmp = docling
    (tmp / "p.md").write_text("pages")
    out = _convert(ctx, {"path": "p.md", "page_range": "1-4"})
    assert out["success"] and out["page_range"] == "1-4"
    conv = [c for c in _calls(log) if "--version" not in c["argv"]][-1]
    assert "--page-range" in conv["argv"]
    assert conv["argv"][conv["argv"].index("--page-range") + 1] == "1-4"
    bad = _convert(ctx, {"path": "p.md", "page_range": "4-1!!"})
    assert bad["success"] is False and "invalid page_range" in bad["error"]


def test_provenance_anchors_and_missing_explicit(docling):
    ctx, _, tmp = docling
    (tmp / "prov.md").write_text("has prov")
    out = _convert(ctx, {"path": "prov.md"})
    assert out["provenance"]["available"] is True
    anchor = out["provenance"]["anchors"][0]
    assert anchor["self_ref"] == "text_1"  # per-item self_ref
    assert anchor["parent"] == "_root_"    # per-item parent
    assert anchor["page_no"] == 1          # nested prov page_no
    assert anchor["charspan"] == [0, 10]   # nested prov charspan
    assert anchor["bbox"] is not None      # nested prov bbox
    assert out["provenance"]["page_provenance"] == "present"
    (tmp / "noprov.md").write_text("NOPROV html-like source")
    out2 = _convert(ctx, {"path": "noprov.md"})
    # HTML: page provenance missing EXPLICITLY, but section/table self_ref+parent
    # references are preserved and uncertainty is stated, not implied.
    assert out2["provenance"]["available"] is True
    assert out2["provenance"]["page_provenance"] == "missing (explicit)"
    refs = {a["self_ref"] for a in out2["provenance"]["anchors"]}
    assert {"section_1", "table_1"} <= refs
    (tmp / "noanch.md").write_text("NOANCHORS NOPROV")
    out3 = _convert(ctx, {"path": "noanch.md"})
    assert out3["provenance"]["available"] is False
    assert "no texts/tables/pictures items with self_ref" in out3["provenance"]["reason"]


def test_bounded_section_selection_verbatim(docling):
    ctx, _, tmp = docling
    (tmp / "sec.md").write_text("NOPROV body text")  # HTML-like: no page prov
    out = _convert(ctx, {
        "path": "sec.md",
        "sections": ["Sec A", "Sec B"],
    })
    selected = out["selected_sections"]
    assert [s["query"] for s in selected] == ["Sec A", "Sec B"]
    assert "alpha" in selected[0]["text"]
    assert selected[0]["text"].startswith("# Sec A")  # verbatim slice
    assert len(selected) <= 10
    assert "page-level provenance unavailable (explicit)" in out["selected_sections_uncertainty"]


# Adversarially-timed fake: mutates the source WHILE conversion runs, so the
# plugin's post-conversion identity recheck (not a stubbed check) does the
# work. The conversion itself is real; only the scheduling is hostile.
FAKE_MUTATES_DURING = FAKE.replace(
    'stem = os.path.splitext(os.path.basename(src))[0]',
    'open(src, "a", encoding="utf-8").write("MUTATED-DURING\\n")\n'
    'stem = os.path.splitext(os.path.basename(src))[0]',
)
FAKE_SLOW = FAKE.replace(
    'stem = os.path.splitext(os.path.basename(src))[0]',
    'import time\n'
    'if "SLOW" in source: time.sleep(1.5)\n'
    'stem = os.path.splitext(os.path.basename(src))[0]',
)


def _fake(tmp_path, name, body):
    fake = tmp_path / name
    fake.write_text(body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def test_mutation_during_conversion_refuses_publication(load_plugin, tmp_path, monkeypatch):
    from tools.terminal_tool import record_session_cwd
    record_session_cwd("task-t", str(tmp_path))
    fake = _fake(tmp_path, "docling-mutate", FAKE_MUTATES_DURING)
    ctx = load_plugin("tool-docling", settings={"binary": str(fake)})
    (tmp_path / "race.md").write_text("hello race")
    out = _convert(ctx, {"path": "race.md"})
    assert out["success"] is False and "identity changed during conversion" in out["error"]
    # Nothing half-published: no marker at all for the raced identity.
    assert not any((ctx.context.state.data_dir / "artifacts").glob("*/artifact.json"))


def test_mutation_during_conversion_preserves_valid_cache(load_plugin, tmp_path, monkeypatch):
    from tools.terminal_tool import record_session_cwd
    record_session_cwd("task-t", str(tmp_path))
    good = _fake(tmp_path, "docling", FAKE)
    ctx = load_plugin("tool-docling", settings={"binary": str(good)})
    (tmp_path / "keep.md").write_text("keep content")
    first = _convert(ctx, {"path": "keep.md"})
    assert first["success"]
    assert first["converter_sha256"]  # executable identity is part of the result
    artifacts = ctx.context.state.data_dir / "artifacts"
    art = artifacts / first["artifact_id"]
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in art.iterdir()}
    mut = _fake(tmp_path, "docling-mutate", FAKE_MUTATES_DURING)
    ctx2 = load_plugin("tool-docling", settings={"binary": str(mut)})
    out = _convert(ctx2, {"path": "keep.md"})
    assert out["success"] is False and "identity changed during conversion" in out["error"]
    # The valid cache entry survives byte-identical; no new entry was created.
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in art.iterdir()}
    assert after == hashes
    assert len(list(artifacts.glob("*/artifact.json"))) == 1


def test_concurrent_same_key_publication_adopted_once(load_plugin, tmp_path, monkeypatch):
    import threading
    from tools.terminal_tool import record_session_cwd
    record_session_cwd("task-t", str(tmp_path))
    fake = _fake(tmp_path, "docling-slow", FAKE_SLOW)
    ctx = load_plugin("tool-docling", settings={"binary": str(fake)})
    (tmp_path / "race2.md").write_text("SLOW shared content")
    results = []
    threads = [threading.Thread(
        target=lambda: results.append(_convert(ctx, {"path": "race2.md"})))
        for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(r["success"] for r in results), results
    # Exactly one fresh publication; the racing writer adopts the valid entry.
    assert sorted(r["from_cache"] for r in results) == [False, True]
    assert results[0]["artifact_id"] == results[1]["artifact_id"]
    art = ctx.context.state.data_dir / "artifacts" / results[0]["artifact_id"]
    marker = json.loads((art / "artifact.json").read_text())
    # Cache-owned fixed names, distinct from the marker; both callers — the
    # winner and the adopting racer — must reference files that exist.
    assert marker["md"] == "document.md" and marker["json"] == "document.json"
    assert (art / "document.md").read_text().startswith("# Title")
    assert json.loads((art / "document.json").read_text())["texts"]
    for r in results:
        assert os.path.exists(r["markdown_path"]) and os.path.exists(r["json_path"])
        assert r["markdown_path"] == str(art / "document.md")
        assert r["json_path"] == str(art / "document.json")
    # Unique staging leaves no shared/leaked temp files behind.
    assert not [p for p in art.iterdir() if p.name.endswith((".part", ".tmp"))]

def test_package_version_change_invalidates_unchanged_launcher_and_platform(load_plugin, tmp_path, monkeypatch):
    from tools.terminal_tool import record_session_cwd
    record_session_cwd("task-t", str(tmp_path))
    fake = tmp_path / "docling-version"
    fake.write_text(FAKE.replace(
        'if "--version" in argv:',
        'if "--version" in argv:\n'
        '    _vf = os.environ.get("FAKE_DOCLING_VERSION_FILE")\n'
        '    if _vf:\n'
        '        exec(compile(open(_vf, encoding="utf-8").read(), "<version>", "exec"), globals())\n'
        '        sys.exit(0)',
    ))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    report = tmp_path / "version-report.py"
    report.write_text("print('Docling version: 2.137.0')\nprint('Platform: Linux')\n")
    monkeypatch.setenv("FAKE_DOCLING_VERSION_FILE", str(report))
    ctx = load_plugin("tool-docling", settings={"binary": str(fake)})
    fake_hash = hashlib.sha256(fake.read_bytes()).hexdigest()
    (tmp_path / "pkg.md").write_text("package identity")
    first = _convert(ctx, {"path": "pkg.md"})
    assert first["success"] and first["converter_sha256"]
    report.write_text("print('Docling version: 2.138.0')\nprint('Platform: Linux')\n")
    upgraded = _convert(ctx, {"path": "pkg.md"})
    assert upgraded["success"] and upgraded["from_cache"] is False
    assert upgraded["artifact_id"] != first["artifact_id"]
    # Launcher bytes and the platform footer both unchanged; only the
    # reported Docling version differs, and that must miss the cache.
    assert upgraded["converter_sha256"] == first["converter_sha256"] == fake_hash


def test_converter_version_change_during_conversion_refuses_publication(load_plugin, tmp_path, monkeypatch):
    from tools.terminal_tool import record_session_cwd
    record_session_cwd("task-t", str(tmp_path))
    fake = tmp_path / "docling-upgrade-race"
    fake.write_text(FAKE.replace(
        'stem = os.path.splitext(os.path.basename(src))[0]',
        'if os.environ.get("FAKE_DOCLING_UPGRADE_TO"):\n'
        '    open(os.environ["FAKE_DOCLING_VERSION_FILE"], "w").write(\n'
        '        "print(\'Docling version: %s\')\\n" % os.environ["FAKE_DOCLING_UPGRADE_TO"])\n'
        'stem = os.path.splitext(os.path.basename(src))[0]',
    ))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    report = tmp_path / "version-report.py"
    report.write_text("print('Docling version: 2.137.0')\n")
    monkeypatch.setenv("FAKE_DOCLING_VERSION_FILE", str(report))
    monkeypatch.setenv("FAKE_DOCLING_UPGRADE_TO", "2.138.0")
    ctx = load_plugin("tool-docling", settings={"binary": str(fake)})
    (tmp_path / "upgrade-race.md").write_text("upgrade race")
    out = _convert(ctx, {"path": "upgrade-race.md"})
    assert out["success"] is False and "identity changed during conversion" in out["error"]
    assert not any((ctx.context.state.data_dir / "artifacts").glob("*/artifact.json"))


def test_platform_footer_or_failed_version_probe_refused(load_plugin, tmp_path, monkeypatch):
    from tools.terminal_tool import record_session_cwd
    record_session_cwd("task-t", str(tmp_path))
    fake = _fake(tmp_path, "docling-invalid-version", FAKE)
    monkeypatch.setenv("FAKE_DOCLING_VERSION_FAIL", "1")
    ctx = load_plugin("tool-docling", settings={"binary": str(fake)})
    (tmp_path / "invalid.md").write_text("must not convert")
    unavailable = _convert(ctx, {"path": "invalid.md"})
    assert unavailable["success"] is False and "version probe failed" in unavailable["error"]
    monkeypatch.setenv("FAKE_DOCLING_VERSION_FAIL", "footer")
    footer_only = _convert(ctx, {"path": "invalid.md"})
    assert footer_only["success"] is False and "version probe failed" in footer_only["error"]
    assert not any((ctx.context.state.data_dir / "artifacts").glob("*/artifact.json"))


def test_artifact_named_source_keeps_marker_and_document_separate(docling):
    ctx, _, tmp = docling
    (tmp / "artifact.md").write_text("prov check")
    out = _convert(ctx, {"path": "artifact.md"})
    assert out["success"]
    assert os.path.realpath(out["markdown_path"]) != os.path.realpath(out["json_path"])
    art = ctx.context.state.data_dir / "artifacts" / out["artifact_id"]
    marker = json.loads((art / "artifact.json").read_text())
    assert marker["json"] != "artifact.json" and marker["md"] != "artifact.json"
    with open(out["json_path"], encoding="utf-8") as fh:
        document = json.load(fh)
    assert document["texts"] and out["provenance"]["available"]


def test_identical_content_different_basename_returns_existing_paths(docling):
    ctx, _, tmp = docling
    (tmp / "first.md").write_text("shared bytes")
    (tmp / "second.md").write_text("shared bytes")
    r1 = _convert(ctx, {"path": "first.md"})
    r2 = _convert(ctx, {"path": "second.md"})
    assert r1["success"] and r2["success"]
    assert r2["from_cache"] is True and r2["artifact_id"] == r1["artifact_id"]
    for r in (r1, r2):
        assert os.path.exists(r["markdown_path"]) and os.path.exists(r["json_path"])
    assert r2["markdown_path"] == r1["markdown_path"]
    with open(r2["json_path"], encoding="utf-8") as fh:
        assert json.load(fh)["texts"]


def test_max_chars_bounds_and_type_enforcement(docling):
    ctx, _, tmp = docling
    (tmp / "x.md").write_text("hello")
    assert _convert(ctx, {"path": "x.md", "max_chars": 0})["text"] == ""
    assert _convert(ctx, {"path": "x.md", "max_chars": -1})["text"] == ""
    big = _convert(ctx, {"path": "x.md", "max_chars": 10**9})
    assert len(big["text"]) <= 100_000
    for bad in ("20000", True, 2.5):
        out = _convert(ctx, {"path": "x.md", "max_chars": bad})
        assert out["success"] is False and "max_chars" in out["error"]
