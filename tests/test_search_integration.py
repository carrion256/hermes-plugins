"""Native Hermes search contracts retained after retiring the redundant adapter.

Exercise the generated consumer, task CWD, credential guards, literal paths,
and bounded search results without a plugin override.
"""

import json
import types
import uuid
import pytest


def _tid():
    """Unique task id per test: isolates repeated-search guard + negative caches."""
    return "native-search-test-" + uuid.uuid4().hex[:12]




@pytest.fixture
def native_search(isolated_hermes):
    from tools.registry import registry

    return types.SimpleNamespace(tool=registry.dispatch)


def test_hermes_tools_stub_routes_search_files_to_native_registry(native_search, tmp_path):
    """Generated hermes_tools consumer: the stub module produced by the real
    generator must route search_files to the native registry dispatch and its
    dict result must carry 'matches' (code_execution stub contract)."""
    from tools.code_execution_tool import generate_hermes_tools_module
    from tools.registry import registry

    (tmp_path / "b.py").write_text("needle_beta marker\n")
    tid = _tid()

    source = generate_hermes_tools_module(["search_files"], transport="uds")
    mod = types.ModuleType("hermes_tools")

    def _route(tool_name, args):
        raw = registry.dispatch(tool_name, args, task_id=tid)
        parsed = json.loads(raw)
        return parsed

    exec(compile(source, "hermes_tools.py", "exec"), mod.__dict__)  # noqa: S102
    # The generated source defines its own _call; replace it AFTER exec so the
    # stub's tool call routes to the actual native registry dispatch.
    mod.__dict__["_call"] = _route

    result = mod.search_files(pattern="needle_beta", path=str(tmp_path))
    assert "matches" in result, result
    assert any("b.py" in str(m["path"]) for m in result["matches"])


# --- actual recorded task CWD ---------------------------------------------------


def test_search_uses_recorded_task_cwd_not_process_cwd(native_search, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text("needle_project_only\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.py").write_text("needle_elsewhere_only\n")
    monkeypatch.chdir(elsewhere)

    import tools.terminal_tool as tt
    from tools.file_tools import _get_file_ops

    h = native_search
    tid = _tid()
    fo = _get_file_ops(tid)
    fo.env.cwd = str(project)
    tt.record_session_cwd(tid, str(project))
    try:
        out = h.tool("search_files", {"pattern": "needle_project", "path": "."}, task_id=tid)
        result = json.loads(out)
        assert result.get("matches"), f"task-cwd search found nothing: {result}"
        text = json.dumps(result)
        assert str(project) in text or "a.py" in text
        assert "needle_elsewhere_only" not in text, "search followed process cwd, not task cwd"
    finally:
        tt.clear_session_cwd(tid)


# --- native guards, errors, bounds ----------------------------------------------


def test_credential_env_file_direct_search_denied_and_directory_redacted(native_search, tmp_path):
    """.env searched directly is denied by the native read block; searching the
    directory must not return the credential value in plaintext."""
    (tmp_path / ".env").write_text("API_TOKEN=supers3cretvalue\n")
    (tmp_path / "safe.txt").write_text("API_TOKEN configured in .env\n")
    h = native_search
    tid = _tid()

    direct = json.loads(h.tool(
        "search_files",
        {"pattern": "API_TOKEN", "path": str(tmp_path / ".env")}, task_id=tid))
    assert direct.get("error"), f"direct .env search must be denied: {direct}"

    directory = json.loads(h.tool(
        "search_files", {"pattern": "API_TOKEN", "path": str(tmp_path)}, task_id=_tid()))
    assert "supers3cretvalue" not in json.dumps(directory), \
        f"credential leaked in directory search: {json.dumps(directory)}"


def test_dash_sentinel_root_not_executed_as_option(native_search, tmp_path):
    """A search root starting with '-' must be handled literally, never reach
    an executable as an option (the removed plugin appended raw paths to argv).
    Task CWD via native session-cwd record, not process chdir."""
    (tmp_path / "-rf.txt").write_text("needle_dash_file\n")
    h = native_search
    import tools.terminal_tool as tt
    from tools.file_tools import _get_file_ops
    tid = _tid()
    fo = _get_file_ops(tid)
    fo.env.cwd = str(tmp_path)
    tt.record_session_cwd(tid, str(tmp_path))
    try:
        result = json.loads(h.tool(
            "search_files",
            {"pattern": "needle_dash_file", "path": "-rf.txt"},
            task_id=tid))
        assert not result.get("error"), f"dash-sentinel root executed: {result}"
        assert result.get("matches"), result
    finally:
        tt.clear_session_cwd(tid)

def test_unknown_path_reports_not_found(native_search, tmp_path):
    h = native_search
    result = json.loads(h.tool(
        "search_files",
        {"pattern": "needle", "path": str(tmp_path / "missing-target.txt")},
        task_id=_tid()))
    assert result.get("error"), f"unknown path must error, not return empty: {result}"
    assert "Path not found" in result["error"] or "missing-target.txt" in result["error"]




def test_file_glob_filter_and_offset_and_global_limit(native_search, tmp_path):
    (tmp_path / "keep.txt").write_text("needle one\nneedle two\n")
    (tmp_path / "skip.log").write_text("needle three\n")
    h = native_search

    globbed = json.loads(h.tool(
        "search_files",
        {"pattern": "needle", "path": str(tmp_path), "file_glob": "*.txt"},
        task_id=_tid()))
    text = json.dumps(globbed)
    assert "skip.log" not in text, f"file_glob did not filter: {text}"
    assert globbed.get("matches"), globbed

    # The result limit applies globally, not once per matching file.
    limited = json.loads(h.tool(
        "search_files",
        {"pattern": "needle", "path": str(tmp_path), "limit": 1},
        task_id=_tid()))
    assert len(limited["matches"]) == 1, f"limit=1 must yield exactly one matching line: {limited}"

    # Content search does not promise cross-file ordering (native `order` is
    # ignored for content). Exercise offset advancement on one ordered file;
    # the multi-file checks above retain the filter/global-limit guarantees.
    pages = [json.loads(h.tool(
        "search_files",
        {"pattern": "needle", "path": str(tmp_path / "keep.txt"), "limit": 1, "offset": off},
        task_id=_tid())) for off in (0, 1, 2)]
    assert all("error" not in page for page in pages), pages
    assert [[match["content"] for match in page.get("matches", [])] for page in pages] == [
        ["needle one"], ["needle two"], [],
    ]


