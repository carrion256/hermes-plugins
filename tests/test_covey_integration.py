"""covey-tasks integration: real Covey CLI + native loader + real agent binding.

COVEY_TEST_BIN is a local prerequisite of this module and tests/test_covey_cli.py
only; it fails loudly (never skips) when absent so the full-suite qualification
cannot silently drop Covey coverage.

Fixture lifetime rules (the lifecycle tests are order- and GC-sensitive):
  * `agent_for(label)` returns ONE strong agent object per label for the whole
    test. A claim is owned by an agent OBJECT; an ephemeral object that is
    garbage collected legitimately exits its claims, so tests must never build
    a throw-away agent per call for anything that holds ownership.
  * `make_runtime` closes every direct CoveyRuntime in guaranteed fixture
    finalization (passing or failing) and checks that runtime's OWN thread.
  * Transport faults are injected with `intercepted(...)`, which patches only
    the runtime transport seam for the block. `monkeypatch.undo()` is never
    used: it would also undo conftest's HERMES_HOME/config isolation.
  * Starting a claim always installs a fresh maintenance Event/thread, so a
    test that drives ticks itself must call `stop_background(rt)` AFTER start.

AC coverage map (plan R3 acceptance criteria):
  AC1  test_registry_exposes_single_tool_and_leaves_native_todo_intact
       test_coexists_with_bundled_covey_both_toolsets_function
  AC2  test_worklist_survives_fresh_instance_and_refuses_append_after_completion
  AC3  test_lists_pages_cover_seeded_ids_once_and_reject_invalid_bounds
       test_lists_projection_has_no_creator_field
       test_reads_never_open_or_register_sessions
       test_reads_add_no_events
  AC4  test_two_agents_race_exactly_one_winner_no_actual_token_leak
       test_wrong_agent_cannot_finish_or_release_owners_claim
       test_parent_and_child_cannot_touch_each_others_claims
       test_compaction_same_object_preserves_ownership
       test_same_object_reset_registers_fresh_principals
       test_self_expiry_same_task_newer_fence_old_finish_rejected
       test_self_expiry_different_task_recovers
       test_live_claim_blocks_different_task_without_stealing
       test_unreadable_claim_status_refuses_different_task_and_keeps_authority
       test_subagent_stop_releases_claim_no_calls_before_stop
       test_finalize_releases_claim
  AC5  test_renewal_many_ticks_and_delayed_tick_keep_headroom_bounded
       test_delayed_tick_past_expiry_retires_not_renews
       test_idle_runtime_issues_nothing_and_ticks_use_fresh_backend_keys
       test_teardown_under_db_lock_bounded_and_no_thread_left
       test_teardown_budget_exhaustion_releases_cwd_routing
       test_claim_publication_atomic_against_close_revocation
  AC6  test_retry_fail_release_abandon_exact_states
       test_required_evidence_refused_without_digest_preserved_with_valid_digest
       test_governed_task_refused_and_policy_unchanged
       test_all_failed_abandoned_worklist_counts_explicit
       test_lease_expired_shown_before_reclaim
  AC7  test_same_request_replay_one_receipt_changed_content_conflicts
       test_invalid_config_rejected_before_subprocess
       test_malformed_output_on_claim_and_start_are_uncertain
       test_committed_then_timeout_is_uncertain_single_effect
       test_register_timeout_reuses_same_principal_and_key
       test_held_db_lock_gives_definite_error_under_5s
       test_transport_timeout_boundary_is_injected_not_lock_timing
       test_reaper_heartbeat_loss_retires_and_stops_renewing
       test_retired_token_replay_refused_new_request_works
       test_partial_claim_start_is_truthful_partial_state
  R3 lifecycle regressions (independent review; AC4/AC5/AC7/AC8):
       test_concurrent_close_bounded_and_no_late_authority
       test_uncertain_register_retry_reuses_principal_and_token
       test_committed_after_timeout_reconcile_replay_one_effect
       test_partial_start_precommit_failure_resumes_on_retry
       test_partial_start_committed_timeout_reports_truthful_state
       test_external_reap_orchestrator_retires_token_then_recovers
       test_external_reap_idle_executor_next_start_recovers
       test_retired_request_refused_after_replacement
       test_retired_request_refused_after_normal_outcome
       test_transient_heartbeat_fault_uncertain_then_recovers
       test_rapid_retire_restart_maintenance_is_heartbeating
       test_exception_text_redacted_in_warning_logs
  AC8  test_own_and_foreign_tokens_never_in_output_or_logs
       test_donor_binary_and_db_env_ignored
       test_concurrent_profile_scopes_keep_distinct_configs_and_databases
       test_ownership_fields_and_model_paths_rejected_before_subprocess
       test_shell_metacharacters_round_trip_literally
       test_literal_dash_and_boundary_priorities_round_trip_via_real_cli
  AC10 test_status_reports_capabilities_never_opens_db_or_registers
       test_stale_binary_refuses_task_operations_with_guidance
       test_on_unload_closes_runtime_and_releases_claim
  R4 workspace-guard custody regressions:
       test_guard_selects_current_database_and_never_leaks_across_dbs
       test_uncertain_unbound_claim_never_rebinds_released_workspace
"""
import contextlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest
import yaml

TOKEN_RE = re.compile(r"session_[0-9a-fA-F]+")
TIMEOUT_ENVELOPE = {"ok": False, "code": "timeout", "message": "timed out",
                    "exit_code": 124}


def _resolve_covey_bin():
    binary = os.environ.get("COVEY_TEST_BIN", "")
    if not binary:
        pytest.fail(
            "COVEY_TEST_BIN is not set. These tests run against the real Covey "
            "binary; point COVEY_TEST_BIN at target/debug/covey "
            "(cargo build --locked --bin covey). No skip: coverage must not "
            "disappear silently."
        )
    resolved = Path(binary).expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        pytest.fail(f"COVEY_TEST_BIN={binary} is not an executable file")
    return str(resolved)


@pytest.fixture
def covey_bin():
    """The resolved real Covey binary; fails loudly instead of skipping."""
    return _resolve_covey_bin()


@pytest.fixture
def covey(load_plugin, covey_bin):
    """Load covey-tasks configured against the real binary; caller supplies db_path."""
    def factory(**overrides):
        settings = {"binary": covey_bin}
        settings.update(overrides)
        return load_plugin("covey-tasks", settings)
    return factory


@pytest.fixture
def agent_for():
    """Stable strong agent per label for the whole test (see module docstring)."""
    held = {}

    def get(session_id):
        agent = held.get(session_id)
        if agent is None:
            agent = held[session_id] = new_agent(session_id)
        return agent
    return get


@pytest.fixture
def make_runtime(covey_bin, tmp_path):
    """Deep runtime-case fixture: real CoveyRuntime, real binary, real agent
    weakref, constructor timing seam. Every runtime created here is closed in
    finalization even when the test fails, and its own maintenance thread must
    be gone afterwards."""
    created = []

    def factory(h, agent, db="d.db", **timing):
        mod = runtime_module(h)
        db_file = tmp_path / db
        rt = mod.CoveyRuntime(agent, binary=covey_bin, db_path=str(db_file),
                              profile_home=str(tmp_path), **timing)
        rt.db_file = db_file  # convenience for the test only
        created.append(rt)
        return rt

    yield factory
    stragglers = []
    for rt in reversed(created):
        rt.close()
        thread = rt._thread
        if thread is not None:
            thread.join(15)
            if thread.is_alive():
                stragglers.append(thread.name)
    assert not stragglers, f"maintenance threads survived runtime close: {stragglers}"


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------

def new_agent(session_id):
    """A real (weakref-able) Hermes agent object, constructed without IO."""
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent.platform = "cli"
    agent._persist_disabled = False
    agent._interrupt_requested = False
    return agent


def runtime_module(h):
    """The runtime.py module object the plugin actually uses (transport seam).

    The adapter registers runtime.py in sys.modules under _covey_tasks_runtime;
    intercepting any *other* copy of the file would patch a dead module, so we
    resolve the live registration and never load a duplicate."""
    import sys
    mod = getattr(h.module, "_runtime_mod", None) or sys.modules.get(
        "_covey_tasks_runtime")
    assert mod is not None, (
        "runtime module not registered — was covey-tasks loaded first?")
    return mod


@contextlib.contextmanager
def intercepted(monkeypatch, mod, fake):
    """Wrap the runtime's transport seam for the duration of the block ONLY.

    `fake(real, args, **kwargs)` receives the transport that was active on
    entry. Leaving the block restores just this patch (scoped monkeypatch
    context); conftest's HERMES_HOME/config isolation stays intact."""
    real = mod.run_covey_command

    def patched(args, **kwargs):
        return fake(real, args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(mod, "run_covey_command", patched)
        yield real


def recording(calls):
    def fake(real, args, **kwargs):
        calls.append(list(args))
        return real(args, **kwargs)
    return fake


def is_cmd(args, *prefix):
    return isinstance(args, (list, tuple)) and tuple(args[:len(prefix)]) == prefix


def flag_value(argv, flag):
    # The runtime may encode a flag as "--flag value" or "--flag=value"
    # (the latter keeps literal values such as "- literal" clap-safe).
    for i, tok in enumerate(argv):
        if tok == flag:
            return argv[i + 1]
        if tok.startswith(flag + "="):
            return tok[len(flag) + 1:]
    raise ValueError(f"flag {flag} not in argv")


def as_obj(raw):
    return raw if isinstance(raw, dict) else json.loads(raw)


def now_ms():
    return int(time.time() * 1000)


def sleep_until_ms(target_ms):
    time.sleep(max(0.0, (target_ms - now_ms()) / 1000.0))


def wait_until(predicate, timeout=10.0, interval=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def stop_background(rt):
    """Stop and join this runtime's background maintenance thread.

    Starting a claim installs a FRESH stop Event and thread, so stopping before
    the start is a no-op; do it after every start, then drive ticks yourself."""
    thread = rt._thread
    assert thread is not None, "no maintenance thread: start a claim first"
    rt._stop_event.set()
    thread.join(15)
    assert not thread.is_alive(), "background maintenance did not stop"


def tick_when_due(rt):
    """Wait (real time) until both cadences are due, then run one real tick."""
    time.sleep(max(rt.heartbeat_interval_ms, rt.renew_interval_ms) / 1000.0 + 0.05)
    rt.maintenance_once()


def lease_deadline(rt):
    """The runtime's authoritative (last returned) lease deadline."""
    claim = ok(rt.execute({"action": "status"}))["active_claim"]
    assert claim is not None, "the runtime holds no active claim"
    return claim["lease_deadline_ms"]


def raw_covey(covey_bin, db_path, *args, timeout=40):
    result = subprocess.run(
        [covey_bin, "--db", str(db_path), "--json", *[str(a) for a in args]],
        capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def fresh_covey(covey_bin, db_path, *args, timeout=40):
    """Run the real CLI against a DB with explicit named JSON argv."""
    rc, stdout, stderr = raw_covey(covey_bin, db_path, *args, timeout=timeout)
    payload = json.loads(stdout or stderr)
    assert rc == 0 and payload.get("ok") is True, (rc, payload)
    return payload.get("data")


def covey_events(covey_bin, db_path):
    """Total domain-event rows (the event table is append-only)."""
    data = fresh_covey(covey_bin, str(db_path), "events", "list",
                       "--limit", "100000")
    rows = data if isinstance(data, list) else data.get("events", [])
    return len(rows)


def db_query(db_path, sql, params=()):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def db_tokens(db_path):
    """Every actual session token issued in this DB (own AND foreign)."""
    try:
        return [row[0] for row in db_query(
            db_path, "SELECT session_token FROM sessions ORDER BY rowid")]
    except sqlite3.OperationalError:
        return []  # DB not initialised at all


def principals(db_path, role):
    """Adapter-minted principals of one role, in registration order."""
    return [row[0] for row in db_query(
        db_path,
        "SELECT agent_principal_id FROM sessions "
        "WHERE agent_principal_id LIKE ? ORDER BY rowid",
        (f"hermes-covey-tasks:{role}:%",))]


def renew_keys(db_path):
    """Backend idempotency keys of every claim renewal that took effect."""
    return [row[0] for row in db_query(
        db_path, "SELECT idempotency_key FROM mutation_idempotency "
                 "WHERE operation = 'renew_claim'")]


def seed_worklist(covey_bin, db_path):
    """Register real orchestrator/executor sessions and seed one available
    direct/default/optional task. Returns (meta_id, subtask_id, orch_token)."""
    orch = fresh_covey(covey_bin, db_path, "session", "register",
                       "--agent-principal-id", "seed-orchestrator",
                       "--agent-instance-id", "seed-i1", "--role", "orchestrator")
    fresh_covey(covey_bin, db_path, "session", "register",
                "--agent-principal-id", "seed-executor",
                "--agent-instance-id", "seed-i2", "--role", "executor")
    meta_id = fresh_covey(covey_bin, db_path, "meta", "submit",
                          "--session-token", orch["session_token"],
                          "--prompt-text", "seed worklist")["meta_task_id"]
    subtask_id = fresh_covey(covey_bin, db_path, "subtask", "create",
                             "--session-token", orch["session_token"],
                             "--meta-task-id", meta_id, "--title", "seed task",
                             "--kind", "work", "--priority", "50",
                             "--completion-policy", "direct",
                             "--evidence-policy", "optional",
                             "--routing-key", "default")["subtask_id"]
    return meta_id, subtask_id, orch["session_token"]


def seed_task(covey_bin, db_path, orch_token, meta_id, title):
    return fresh_covey(covey_bin, db_path, "subtask", "create",
                       "--session-token", orch_token,
                       "--meta-task-id", meta_id, "--title", title,
                       "--completion-policy", "direct",
                       "--evidence-policy", "optional",
                       "--routing-key", "default")["subtask_id"]


def assert_no_tokens(serialized, actual_tokens=()):
    """Neither the token grammar nor any actual issued token may appear."""
    for token in actual_tokens:
        assert token not in serialized, f"actual token leaked: {serialized}"
    assert not TOKEN_RE.search(serialized), serialized


def call(h, agent, **arguments):
    from agent.subagent_lifecycle import bind_subagent_parent
    with bind_subagent_parent(agent):
        return json.loads(h.tool("covey_tasks", arguments))


def ok(result):
    assert result.get("ok") is True, result
    return result["data"]


def err(result):
    assert result.get("ok") is False, result
    return result


# ---------------------------------------------------------------------------
# AC1: registration and coexistence
# ---------------------------------------------------------------------------

def test_registry_exposes_single_tool_and_leaves_native_todo_intact(covey, tmp_path, agent_for):
    import importlib
    import tools.todo_tool as native_todo  # the real native module registers todo_list
    from tools.registry import registry
    if registry.get_entry("todo_list") is None:
        # Registration happens at first import per process; an earlier test's
        # registry restore can have removed it while the module stays cached.
        native_todo = importlib.reload(native_todo)
    native_entry = registry.get_entry("todo_list")
    assert native_entry is not None and native_entry.toolset == "todo"

    h = covey(db_path=str(tmp_path / "d.db"))

    entry = registry.get_entry("covey_tasks")
    assert entry is not None and entry.toolset == "covey-tasks"
    assert registry.get_tool_names_for_toolset("covey-tasks") == ["covey_tasks"]
    assert registry.get_entry("todo_list") is native_entry  # not replaced or wrapped

    # The native todo tool still behaves natively through the registry...
    store = native_todo.TodoStore()
    plan = [{"id": "plan", "content": "native step", "status": "in_progress"}]
    written = as_obj(registry.dispatch("todo_list", {"todos": plan}, store=store))
    assert [t["id"] for t in written["todos"]] == ["plan"]
    assert written["summary"]["in_progress"] == 1
    snapshot = store.snapshot()

    # ...and Covey tool calls alone never write into native todos.
    agent = agent_for("s1")
    meta_id = ok(call(h, agent, action="create", prompt="durable work"))["meta_task_id"]
    ok(call(h, agent, action="add", meta_task_id=meta_id, title="durable step"))
    assert store.snapshot() == snapshot
    reread = as_obj(registry.dispatch("todo_list", {}, store=store))
    assert [t["id"] for t in reread["todos"]] == ["plan"]


def test_coexists_with_bundled_covey_both_toolsets_function(covey, covey_bin, tmp_path, agent_for):
    import plugins.covey as bundled
    from hermes_cli.plugins_discovery import scan_directory
    from tools.registry import registry
    db = tmp_path / "shared.db"
    h = covey(db_path=str(db))
    manifests = {m.name: m for m in scan_directory(Path(bundled.__file__).parent.parent, "user")}
    h.manager._load_plugin(manifests["covey"])
    loaded = h.manager._plugins["covey"]
    assert loaded.enabled, loaded.error
    # Both toolsets are registered side by side; neither shadows the other.
    assert registry.get_entry("covey_tasks").toolset == "covey-tasks"
    assert registry.get_entry("covey_subtask_status").toolset == "covey"

    agent = agent_for("owner")
    meta_id = ok(call(h, agent, action="create", prompt="shared"))["meta_task_id"]
    tid = ok(call(h, agent, action="add", meta_task_id=meta_id,
                  title="visible to both"))["subtask_id"]
    # The bundled low-level toolset reads the same task covey-tasks created...
    probe = {"subtask_id": tid, "db_path": str(db), "binary": covey_bin}
    before = as_obj(h.tool("covey_subtask_status", probe))
    assert before["ok"] is True and before["data"]["subtask"]["state"] == "available"
    # ...and sees every transition covey-tasks drives, with both loaded.
    ok(call(h, agent, action="start", subtask_id=tid))
    during = as_obj(h.tool("covey_subtask_status", probe))
    assert during["data"]["subtask"]["state"] == "in_progress"
    ok(call(h, agent, action="finish", subtask_id=tid, summary="done"))
    after = as_obj(h.tool("covey_subtask_status", probe))
    assert after["data"]["subtask"]["state"] == "completed"
    shown = ok(call(h, agent, action="show", subtask_id=tid))
    assert shown["subtask"]["state"] == "completed"


# ---------------------------------------------------------------------------
# AC2: durability
# ---------------------------------------------------------------------------

def test_worklist_survives_fresh_instance_and_refuses_append_after_completion(covey, tmp_path, agent_for):
    db = tmp_path / "dur.db"
    h = covey(db_path=str(db))
    a = agent_for("s1")
    meta_id = ok(call(h, a, action="create", prompt="ship the widget"))["meta_task_id"]
    t1 = ok(call(h, a, action="add", meta_task_id=meta_id, title="build", priority=50))["subtask_id"]
    t2 = ok(call(h, a, action="add", meta_task_id=meta_id, title="test"))["subtask_id"]
    receipts = []
    for tid, summary in ((t1, "built"), (t2, "tested")):
        ok(call(h, a, action="start", subtask_id=tid))
        fin = call(h, a, action="finish", subtask_id=tid, summary=summary)
        assert fin["ok"] is True, fin
        receipts.append(fin["request_id"])
    assert len(set(receipts)) == 2  # separate receipts
    # Fresh plugin instance (restart analogue) on the same DB: exact persisted state.
    h2 = covey(db_path=str(db))
    reader = agent_for("s2")
    shown = ok(call(h2, reader, action="show", meta_task_id=meta_id))
    assert {t["subtask_id"]: t["state"] for t in shown["subtasks"]} == \
        {t1: "completed", t2: "completed"}
    shown_t1 = ok(call(h2, reader, action="show", subtask_id=t1))
    assert shown_t1["subtask"]["state"] == "completed"
    assert any(o["summary"] == "built" for o in shown_t1["attempt_outcomes"])
    shown_t2 = ok(call(h2, reader, action="show", subtask_id=t2))
    assert any(o["summary"] == "tested" for o in shown_t2["attempt_outcomes"])
    # Append to a completed worklist: refused, nothing created (no rollover).
    err(call(h2, reader, action="add", meta_task_id=meta_id, title="late"))
    shown_after = ok(call(h2, reader, action="show", meta_task_id=meta_id))
    assert len(shown_after["subtasks"]) == 2
    assert len(ok(call(h2, reader, action="lists"))["items"]) == 1
    # No local mirror: state lives only in the configured DB.
    assert not any((tmp_path / "hermes-home").rglob("todos*"))


# ---------------------------------------------------------------------------
# AC3: discovery
# ---------------------------------------------------------------------------

def test_lists_pages_cover_seeded_ids_once_and_reject_invalid_bounds(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    a, reader = agent_for("s1"), agent_for("s2")
    ids = [ok(call(h, a, action="create", prompt=f"worklist {i}"))["meta_task_id"]
           for i in range(3)]
    page1 = ok(call(h, reader, action="lists", limit=2, offset=0))
    assert len(page1["items"]) == 2 and page1["limit"] == 2 and page1["offset"] == 0
    page2 = ok(call(h, reader, action="lists", limit=2, offset=2))
    assert len(page2["items"]) == 1
    items = page1["items"] + page2["items"]
    seen = [i["meta_task_id"] for i in items]
    assert sorted(seen) == sorted(ids)  # once, no dupes, no misses
    # Deterministic order: created_at DESC, meta_task_id DESC, same as the engine.
    keys = [(i["created_at"], i["meta_task_id"]) for i in items]
    assert keys == sorted(keys, reverse=True)
    engine = [row["meta_task_id"]
              for row in fresh_covey(covey_bin, db, "meta", "list", "--limit", "3")]
    assert seen == engine
    for bad in ({"limit": 0}, {"limit": 501}, {"offset": -1}, {"limit": "x"},
                {"limit": True}):
        err(call(h, reader, action="lists", **bad))
    # Nonexistent DB: not_initialized, and the read creates no file.
    missing = tmp_path / "empty.db"
    out = err(call(covey(db_path=str(missing)), agent_for("s3"), action="lists"))
    assert out["code"] == "not_initialized", out
    assert not missing.exists()
    # Engine-initialised DB with no worklists: the list is exactly [] (not an error).
    empty_db = tmp_path / "raw-empty.db"
    assert fresh_covey(covey_bin, empty_db, "meta", "list") == []
    listed = ok(call(covey(db_path=str(empty_db)), agent_for("s4"), action="lists"))
    assert listed["items"] == []


def test_lists_projection_has_no_creator_field(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    a = agent_for("s1")
    seed_worklist(covey_bin, db)  # foreign creator session really exists
    for _ in range(2):
        out = call(h, a, action="lists")
        items = ok(out)["items"]
        assert len(items) == 1
        for item in items:
            assert set(item) == {"meta_task_id", "prompt_text", "state",
                                 "created_at", "updated_at"}
        assert_no_tokens(json.dumps(out), actual_tokens=db_tokens(db))


def test_reads_never_open_or_register_sessions(covey, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    a, reader = agent_for("s1"), agent_for("s2")
    # Reads on a nonexistent DB: not_initialized, and the engine is never
    # opened (it would otherwise auto-create and migrate).
    lists = call(h, a, action="lists")
    err(lists)
    assert lists["code"] == "not_initialized", lists
    assert not db.exists()
    # Explicit create does open/create the DB.
    meta_id = ok(call(h, a, action="create", prompt="go"))["meta_task_id"]
    assert db.exists()
    tokens_before = db_tokens(db)
    ok(call(h, reader, action="lists"))
    err(call(h, reader, action="show", meta_task_id="m_missing"))
    ok(call(h, reader, action="show", meta_task_id=meta_id))
    assert db_tokens(db) == tokens_before  # reads register nothing new


def test_reads_add_no_events(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    a = agent_for("s1")
    meta_id = ok(call(h, a, action="create", prompt="events"))["meta_task_id"]
    base = covey_events(covey_bin, db)
    for agent in (a, agent_for("s2")):
        ok(call(h, agent, action="lists"))
        ok(call(h, agent, action="show", meta_task_id=meta_id))
        ok(call(h, agent, action="status"))
    assert covey_events(covey_bin, db) == base


# ---------------------------------------------------------------------------
# AC4: ownership
# ---------------------------------------------------------------------------

def test_two_agents_race_exactly_one_winner_no_actual_token_leak(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    agents = {"a1": agent_for("s1"), "a2": agent_for("s2")}
    results = {}
    gate = threading.Event()

    def racer(label):
        gate.wait(10)
        results[label] = call(h, agents[label], action="start", subtask_id=tid)

    threads = [threading.Thread(target=racer, args=(label,)) for label in agents]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads)
    assert set(results) == set(agents), results
    winners = [label for label, r in results.items() if r.get("ok") is True]
    assert len(winners) == 1, results
    winner = winners[0]
    loser = next(label for label in agents if label != winner)
    # The loser's output must not carry the winner's ACTUAL token.
    assert_no_tokens(json.dumps(results[loser]), actual_tokens=db_tokens(db))
    # Exactly one valid claim: only the winner can finish; the loser cannot.
    err(call(h, agents[loser], action="finish", subtask_id=tid, summary="not mine"))
    assert ok(call(h, agents[winner], action="finish", subtask_id=tid,
                   summary="mine"))["state"] == "completed"


def test_wrong_agent_cannot_finish_or_release_owners_claim(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    owner, intruder, watcher = agent_for("owner"), agent_for("intruder"), agent_for("watcher")
    ok(call(h, owner, action="start", subtask_id=tid))
    err(call(h, intruder, action="finish", subtask_id=tid, summary="stolen"))
    err(call(h, intruder, action="release", subtask_id=tid))
    shown = ok(call(h, watcher, action="show", subtask_id=tid))
    assert shown["subtask"]["state"] == "in_progress"
    assert shown["ownership_health"] == "held"
    assert shown["attempt_outcomes"] == []  # no receipt was written by the intruder


def test_parent_and_child_cannot_touch_each_others_claims(covey, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    parent, child = agent_for("parent"), agent_for("child")
    meta_id = ok(call(h, parent, action="create", prompt="shared plan"))["meta_task_id"]
    t1 = ok(call(h, parent, action="add", meta_task_id=meta_id, title="parent task"))["subtask_id"]
    t2 = ok(call(h, parent, action="add", meta_task_id=meta_id, title="child task"))["subtask_id"]
    ok(call(h, parent, action="start", subtask_id=t1))
    ok(call(h, child, action="start", subtask_id=t2))
    # Independent per-role principals for parent and child in the same profile.
    assert len(set(principals(db, "executor"))) == 2
    err(call(h, child, action="finish", subtask_id=t1, summary="parent's?"))
    err(call(h, parent, action="release", subtask_id=t2))
    # Both claims live side by side; each owner finishes its own work.
    assert ok(call(h, parent, action="finish", subtask_id=t1,
                   summary="mine"))["state"] == "completed"
    assert ok(call(h, child, action="finish", subtask_id=t2,
                   summary="also mine"))["state"] == "completed"


def test_compaction_same_object_preserves_ownership(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    agent = agent_for("s1")
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    ok(call(h, agent, action="start", subtask_id=tid))
    agent.session_id = "rotated-after-compaction"  # same object, rotated id
    res = call(h, agent, action="finish", subtask_id=tid, summary="ok after rotation")
    assert res["ok"] is True, res
    assert len(principals(db, "executor")) == 1  # no re-registration after rotation


def test_same_object_reset_registers_fresh_principals(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    agent = agent_for("s1")
    meta_id, t1, _ = seed_worklist(covey_bin, db)
    ok(call(h, agent, action="start", subtask_id=t1))
    first_executor = principals(db, "executor")
    assert len(first_executor) == 1
    h.hook("on_session_reset", session_id="s1")
    h2 = covey(db_path=str(db))  # fresh runtime on the SAME live agent object
    t2 = ok(call(h2, agent, action="add", meta_task_id=meta_id, title="next"))["subtask_id"]
    start = call(h2, agent, action="start", subtask_id=t2)
    assert start["ok"] is True, start  # fresh principals; no SessionAlreadyActive
    executors = principals(db, "executor")
    assert len(executors) == 2 and executors[0] == first_executor[0]
    assert executors[1] != executors[0]  # a fresh per-role principal after reset
    assert len(principals(db, "orchestrator")) == 1


def test_self_expiry_same_task_newer_fence_old_finish_rejected(covey, covey_bin, agent_for, make_runtime):
    # Maintenance stopped; the engine's own lease expiry does the retiring
    # (no daemon needed). Honest short lease: 400 ms.
    h = covey(db_path="/unused/placeholder.db")
    rt = make_runtime(h, agent_for("live-agent"), lease_duration_ms=400,
                      renew_interval_ms=10000, heartbeat_interval_ms=10000)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    first = ok(rt.execute({"action": "start", "subtask_id": tid}))
    stop_background(rt)  # maintenance genuinely stopped; no ticks deliver
    sleep_until_ms(first["lease_deadline_ms"] + 500)  # real expiry of the real lease
    # The lapsed claimant can no longer operate on the task: the stale
    # authority is refused, not silently honored.
    stale_finish = rt.execute({"action": "finish", "subtask_id": tid,
                               "summary": "stale attempt"})
    assert stale_finish["ok"] is False, stale_finish
    # The SAME still-live agent self-recovers on the SAME task with a
    # strictly newer fence.
    recovered = ok(rt.execute({"action": "start", "subtask_id": tid}))
    assert recovered["fence_seq"] > first["fence_seq"]
    assert recovered["claim_id"] != first["claim_id"]
    shown = ok(rt.execute({"action": "show", "subtask_id": tid}))
    assert shown["ownership_health"] == "held"
    assert ok(rt.execute({"action": "finish", "subtask_id": tid,
                          "summary": "kept"}))["state"] == "completed"
    # The superseded principal was exited: the receipt is the new owner's only.
    shown = ok(rt.execute({"action": "show", "subtask_id": tid}))
    assert [o["summary"] for o in shown["attempt_outcomes"]] == ["kept"]


def test_self_expiry_different_task_recovers(covey, covey_bin, agent_for, make_runtime):
    h = covey(db_path="/unused/placeholder.db")
    rt = make_runtime(h, agent_for("live-agent"), lease_duration_ms=400,
                      renew_interval_ms=10000, heartbeat_interval_ms=10000)
    meta_id, tid, orch = seed_worklist(covey_bin, rt.db_file)
    other = seed_task(covey_bin, rt.db_file, orch, meta_id, "other task")
    rid = str(uuid.uuid4())
    first = ok(rt.execute({"action": "start", "subtask_id": tid, "request_id": rid}))
    stop_background(rt)
    sleep_until_ms(first["lease_deadline_ms"] + 500)  # real expiry
    # Start of a DIFFERENT available task succeeds after self-expiry: the
    # stale claim is retired and a fresh executor claims the other task.
    recovered = ok(rt.execute({"action": "start", "subtask_id": other}))
    assert recovered["subtask_id"] == other and recovered["state"] == "in_progress"
    assert recovered["claim_id"] != first["claim_id"]
    assert len(set(principals(rt.db_file, "executor"))) == 2  # fresh principal
    old = ok(rt.execute({"action": "show", "subtask_id": tid}))
    assert old["subtask"]["state"] == "available"  # the retired claim was released
    assert old["ownership_health"] == "unclaimed"
    held = ok(rt.execute({"action": "show", "subtask_id": other}))
    assert held["ownership_health"] == "held"
    # The original request is bound to the retired token: replay is refused,
    # never replayed under the fresh token.
    replay = rt.execute({"action": "start", "subtask_id": tid, "request_id": rid})
    assert replay["ok"] is False and replay["code"] == "replay_refused", replay
    assert ok(rt.execute({"action": "show", "subtask_id": tid}))[
        "subtask"]["state"] == "available"


def test_live_claim_blocks_different_task_without_stealing(covey, covey_bin, agent_for, make_runtime):
    h = covey(db_path="/unused/placeholder.db")
    rt = make_runtime(h, agent_for("live-agent"), lease_duration_ms=300000,
                      renew_interval_ms=60000, heartbeat_interval_ms=60000)
    meta_id, tid, orch = seed_worklist(covey_bin, rt.db_file)
    other = seed_task(covey_bin, rt.db_file, orch, meta_id, "other task")
    first = ok(rt.execute({"action": "start", "subtask_id": tid}))
    refused = rt.execute({"action": "start", "subtask_id": other})
    assert refused["ok"] is False and refused["code"] == "active_claim_held", refused
    assert refused["data"]["active_subtask_id"] == tid
    # Nothing was stolen or released: the same claim is still held, the other
    # task untouched, and the owner can still finish with its original authority.
    held = ok(rt.execute({"action": "show", "subtask_id": tid}))
    assert held["subtask"]["state"] == "in_progress" and held["ownership_health"] == "held"
    assert ok(rt.execute({"action": "show", "subtask_id": other}))[
        "subtask"]["state"] == "available"
    assert lease_deadline(rt) == first["lease_deadline_ms"]
    assert len(principals(rt.db_file, "executor")) == 1
    assert ok(rt.execute({"action": "finish", "subtask_id": tid,
                          "summary": "still mine"}))["state"] == "completed"


def test_unreadable_claim_status_refuses_different_task_and_keeps_authority(
        covey, covey_bin, agent_for, make_runtime, monkeypatch):
    h = covey(db_path="/unused/placeholder.db")
    mod = runtime_module(h)
    rt = make_runtime(h, agent_for("live-agent"), lease_duration_ms=300000,
                      renew_interval_ms=60000, heartbeat_interval_ms=60000)
    meta_id, tid, orch = seed_worklist(covey_bin, rt.db_file)
    other = seed_task(covey_bin, rt.db_file, orch, meta_id, "other task")
    first = ok(rt.execute({"action": "start", "subtask_id": tid}))

    def unreadable_status(real, args, **kwargs):
        if is_cmd(args, "subtask", "status"):
            return dict(TIMEOUT_ENVELOPE)
        return real(args, **kwargs)

    with intercepted(monkeypatch, mod, unreadable_status):
        refused = rt.execute({"action": "start", "subtask_id": other})
    assert refused["ok"] is False and refused["code"] == "outcome_unknown", refused
    # Authority could not be disproved, so nothing was retired or replaced.
    status = ok(rt.execute({"action": "status"}))
    assert status["active_claim"]["claim_id"] == first["claim_id"]
    assert status["lost_reason"] is None and status["executor_registered"] is True
    assert len(principals(rt.db_file, "executor")) == 1
    assert ok(rt.execute({"action": "show", "subtask_id": other}))[
        "subtask"]["state"] == "available"
    assert ok(rt.execute({"action": "finish", "subtask_id": tid,
                          "summary": "authority intact"}))["state"] == "completed"


def test_subagent_stop_releases_claim_no_calls_before_stop(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    h = covey(db_path=str(tmp_path / "d.db"))
    mod = runtime_module(h)
    calls = []
    meta_id, tid, _ = seed_worklist(covey_bin, tmp_path / "d.db")
    parent, child = agent_for("parent"), agent_for("child")
    with intercepted(monkeypatch, mod, recording(calls)):
        # Child claims its own task through the adapter (adapter-owned runtime).
        ok(call(h, parent, action="start", subtask_id=tid))
        t2 = ok(call(h, parent, action="add", meta_task_id=meta_id,
                     title="child task"))["subtask_id"]
        ok(call(h, child, action="start", subtask_id=t2))
        calls_after_start = len(calls)
        # Idle between stop and hook: no backend call goes out.
        time.sleep(0.3)
        assert len(calls) == calls_after_start, calls[calls_after_start:]
        # Session_id rotation (compaction-like) before the stop hook: live
        # weakref session id must still resolve the child runtime.
        child.session_id = "rotated-child-id"
        h.hook("subagent_stop", child_session_id="rotated-child-id")
        post = calls[calls_after_start:]
        assert post, "stop hook issued no backend call"
        assert all(c[:2] == ["session", "exit"] for c in post), post
        # The child's task is released even though the child object stays reachable.
        shown = ok(call(h, parent, action="show", subtask_id=t2))
        assert shown["ownership_health"] == "unclaimed"
        assert shown["subtask"]["state"] == "available"
        # The parent's own claim is untouched by the child's stop.
        mine = ok(call(h, parent, action="show", subtask_id=tid))
        assert mine["ownership_health"] == "held"
        # The still-reachable child object cannot act on the released claim.
        late = call(h, child, action="finish", subtask_id=t2, summary="late")
        err(late)


def test_finalize_releases_claim(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    agent = agent_for("s1")
    ok(call(h, agent, action="start", subtask_id=tid))
    h.hook("on_session_finalize", session_id="s1")
    shown = ok(call(h, agent_for("s2"), action="show", subtask_id=tid))
    assert shown["subtask"]["state"] == "available"
    assert shown["ownership_health"] == "unclaimed"


# ---------------------------------------------------------------------------
# AC5: long work under maintenance
# ---------------------------------------------------------------------------

def test_renewal_many_ticks_and_delayed_tick_keep_headroom_bounded(covey, covey_bin, agent_for, make_runtime):
    h = covey(db_path="/unused/placeholder.db")
    lease_ms, cadence_ms, margin = 3000, 400, 1200
    rt = make_runtime(h, agent_for("s1"), lease_duration_ms=lease_ms,
                      renew_interval_ms=cadence_ms, heartbeat_interval_ms=cadence_ms)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    started_at = time.monotonic()
    ok(rt.execute({"action": "start", "subtask_id": tid}))
    stop_background(rt)  # the test drives every tick from here
    keys_before = set(renew_keys(rt.db_file))
    previous = lease_deadline(rt)
    for tick in range(8):
        if tick == 4:
            time.sleep(cadence_ms / 1000.0)  # one delayed tick
        tick_when_due(rt)
        observed = now_ms()
        deadline = lease_deadline(rt)  # the authoritative deadline returned by THIS tick
        assert observed + lease_ms - cadence_ms - margin <= deadline \
            <= observed + lease_ms + margin, (tick, observed, deadline)
        assert deadline > previous, (tick, previous, deadline)  # renewal really extended
        previous = deadline
    # Every tick took backend effect under its own fresh idempotency key, and
    # the backend's lease agrees with the returned deadline.
    assert len(set(renew_keys(rt.db_file)) - keys_before) >= 8
    task = fresh_covey(covey_bin, rt.db_file, "subtask", "status", "--subtask-id", tid)
    assert task["claim"]["lease_deadline"] == previous
    # The real lease was outlived only because maintenance renewed it.
    assert time.monotonic() - started_at > lease_ms / 1000.0
    fin = rt.execute({"action": "finish", "subtask_id": tid, "summary": "long work"})
    assert fin["ok"] is True, fin


def test_delayed_tick_past_expiry_retires_not_renews(covey, covey_bin, agent_for, make_runtime):
    h = covey(db_path="/unused/placeholder.db")
    rt = make_runtime(h, agent_for("s1"), lease_duration_ms=1500,
                      renew_interval_ms=300, heartbeat_interval_ms=300)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    first = ok(rt.execute({"action": "start", "subtask_id": tid}))
    stop_background(rt)
    tick_when_due(rt)  # in time: renewal succeeds
    in_time = lease_deadline(rt)
    assert in_time > first["lease_deadline_ms"]
    assert renew_keys(rt.db_file)  # the in-time renewal really took effect
    sleep_until_ms(in_time + 300)  # the delayed tick arrives after real expiry
    rt.maintenance_once()          # renewal is refused; must retire, not fake success
    status = ok(rt.execute({"action": "status"}))
    assert status["active_claim"] is None and status["lost_reason"] is not None
    assert status["maintenance_running"] is False
    # The backend lease was never extended past the in-time renewal.
    backend = fresh_covey(covey_bin, rt.db_file, "subtask", "status", "--subtask-id", tid)
    assert backend["claim"]["lease_deadline"] == in_time
    shown = ok(rt.execute({"action": "show", "subtask_id": tid}))
    assert shown["ownership_health"] == "lease_expired"
    # The same still-live agent recovers by explicitly reclaiming (newer fence).
    again = ok(rt.execute({"action": "start", "subtask_id": tid}))
    assert again["fence_seq"] > first["fence_seq"]
    assert again["claim_id"] != first["claim_id"]
    shown2 = ok(rt.execute({"action": "show", "subtask_id": tid}))
    assert shown2["ownership_health"] == "held"


def test_idle_runtime_issues_nothing_and_ticks_use_fresh_backend_keys(
        covey, covey_bin, agent_for, make_runtime, monkeypatch):
    h = covey(db_path="/unused/placeholder.db")
    mod = runtime_module(h)
    rt = make_runtime(h, agent_for("idle"), lease_duration_ms=2000,
                      renew_interval_ms=100, heartbeat_interval_ms=100)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    base = covey_events(covey_bin, rt.db_file)
    issued = []
    with intercepted(monkeypatch, mod, recording(issued)):
        for _ in range(3):
            time.sleep(0.15)
            rt.maintenance_once()
    assert issued == []  # idle: not a single backend call
    assert covey_events(covey_bin, rt.db_file) == base  # idle: no domain events
    # Live claim: every renewal is a fresh backend mutation, never a replayed one.
    ok(rt.execute({"action": "start", "subtask_id": tid}))
    stop_background(rt)
    keys_before = set(renew_keys(rt.db_file))
    events_before = covey_events(covey_bin, rt.db_file)
    for _ in range(4):
        tick_when_due(rt)
    fresh = set(renew_keys(rt.db_file)) - keys_before
    assert len(fresh) >= 4, fresh
    assert covey_events(covey_bin, rt.db_file) >= events_before + 4
    task = fresh_covey(covey_bin, rt.db_file, "subtask", "status", "--subtask-id", tid)
    assert task["claim"]["lease_deadline"] == lease_deadline(rt)


def test_teardown_under_db_lock_bounded_and_no_thread_left(covey, covey_bin, agent_for, make_runtime):
    h = covey(db_path="/unused/placeholder.db")
    rt = make_runtime(h, agent_for("s1"), lease_duration_ms=300000,
                      renew_interval_ms=100, heartbeat_interval_ms=100,
                      teardown_timeout=2.0)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    ok(rt.execute({"action": "start", "subtask_id": tid}))
    maint = rt._thread  # this runtime's own maintenance thread
    assert maint is not None and maint.is_alive()
    holder = sqlite3.connect(str(rt.db_file), timeout=0)
    holder.execute("PRAGMA busy_timeout=0")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE _blocker (x INTEGER)")
    try:
        t0 = time.monotonic()
        rt.close()
        elapsed = time.monotonic() - t0
        assert elapsed < 4.0, f"teardown exceeded deadline: {elapsed:.1f}s"
    finally:
        holder.rollback()
        holder.close()
    assert rt.closed is True
    maint.join(15)
    assert not maint.is_alive()  # no continuing maintenance thread


# ---------------------------------------------------------------------------
# AC6: semantics
# ---------------------------------------------------------------------------

def test_retry_fail_release_abandon_exact_states(covey, tmp_path, agent_for):
    h = covey(db_path=str(tmp_path / "d.db"))
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id = ok(call(h, agent, action="create", prompt="semantics"))["meta_task_id"]
    # Every task is planned BEFORE any finishes: the worklist closes once all
    # of its tasks are terminal, and a closed worklist rejects new tasks.
    t1 = ok(call(h, agent, action="add", meta_task_id=meta_id, title="retry me"))["subtask_id"]
    t2 = ok(call(h, agent, action="add", meta_task_id=meta_id, title="release me"))["subtask_id"]
    t3 = ok(call(h, agent, action="add", meta_task_id=meta_id, title="abandon me"))["subtask_id"]
    ok(call(h, agent, action="start", subtask_id=t1))
    retry = call(h, agent, action="retry", subtask_id=t1, summary="flaked", failure_code="FLAKE")
    assert retry["ok"] is True, retry
    assert retry["data"]["state"] == "available"
    ok(call(h, agent, action="start", subtask_id=t1))
    fail = call(h, agent, action="fail", subtask_id=t1, summary="broke", failure_code="BAD")
    assert fail["ok"] is True, fail
    assert fail["data"]["state"] == "failed"
    ok(call(h, agent, action="start", subtask_id=t2))
    rel = call(h, agent, action="release", subtask_id=t2)
    assert rel["ok"] is True and rel["data"]["state"] == "available", rel
    ok(call(h, agent, action="start", subtask_id=t3))
    ab = call(h, agent, action="abandon", subtask_id=t3)
    assert ab["ok"] is True and ab["data"]["state"] == "abandoned", ab
    # Summaries/failure codes persisted truthfully in attempt outcomes.
    shown1 = ok(call(h, reader, action="show", subtask_id=t1))
    assert shown1["subtask"]["state"] == "failed"
    kinds = {o["outcome_kind"] for o in shown1["attempt_outcomes"]}
    assert kinds == {"retryable_failure", "terminal_failure"}
    assert any(o["summary"] == "flaked" and o["failure_code"] == "FLAKE"
               for o in shown1["attempt_outcomes"])
    assert any(o["summary"] == "broke" and o["failure_code"] == "BAD"
               for o in shown1["attempt_outcomes"])
    # Release and abandon write no receipt; the released task is claimable again.
    assert ok(call(h, reader, action="show", subtask_id=t2))["subtask"]["state"] == "available"
    shown3 = ok(call(h, reader, action="show", subtask_id=t3))
    assert shown3["subtask"]["state"] == "abandoned" and shown3["attempt_outcomes"] == []
    meta = ok(call(h, reader, action="show", meta_task_id=meta_id))
    assert meta["counts"] == {"failed": 1, "available": 1, "abandoned": 1}


def test_required_evidence_refused_without_digest_preserved_with_valid_digest(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "req.db"
    meta_id, tid, orch = seed_worklist(covey_bin, db)
    tid = fresh_covey(covey_bin, db, "subtask", "create",
                      "--session-token", orch,
                      "--meta-task-id", meta_id, "--title", "needs evidence",
                      "--completion-policy", "direct",
                      "--evidence-policy", "required",
                      "--routing-key", "default")["subtask_id"]
    h = covey(db_path=str(db))
    agent, reader = agent_for("s1"), agent_for("s2")
    ok(call(h, agent, action="start", subtask_id=tid))
    refused = call(h, agent, action="finish", subtask_id=tid, summary="no digest")
    err(refused)
    # No fabricated digest, no receipt: the claim is still ours and held.
    shown = ok(call(h, reader, action="show", subtask_id=tid))
    assert shown["subtask"]["state"] == "in_progress"
    assert shown["attempt_outcomes"] == []
    digest = "sha256:" + "ab" * 32
    fin = call(h, agent, action="finish", subtask_id=tid, summary="done",
               evidence_digest=digest)
    assert fin["ok"] is True, fin
    shown2 = ok(call(h, reader, action="show", subtask_id=tid))
    assert shown2["subtask"]["state"] == "completed"
    assert any(o["evidence_digest"] == digest for o in shown2["attempt_outcomes"])


def test_governed_task_refused_and_policy_unchanged(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "gov.db"
    meta_id, tid, orch = seed_worklist(covey_bin, db)
    tid = fresh_covey(covey_bin, db, "subtask", "create",
                      "--session-token", orch,
                      "--meta-task-id", meta_id, "--title", "reviewed task",
                      "--completion-policy", "reviewed",
                      "--evidence-policy", "optional",
                      "--routing-key", "default")["subtask_id"]
    h = covey(db_path=str(db))
    result = call(h, agent_for("s1"), action="start", subtask_id=tid)
    err(result)
    shown = ok(call(h, agent_for("s2"), action="show", subtask_id=tid))
    assert shown["subtask"]["state"] == "available"  # refused before claiming
    assert shown["subtask"]["completion_policy"] == "reviewed"  # policy unchanged


def test_all_failed_abandoned_worklist_counts_explicit(covey, tmp_path, agent_for):
    h = covey(db_path=str(tmp_path / "d.db"))
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id = ok(call(h, agent, action="create", prompt="all bad"))["meta_task_id"]
    # Plan every task first: the last terminal outcome closes the worklist.
    t1 = ok(call(h, agent, action="add", meta_task_id=meta_id, title="fail me"))["subtask_id"]
    t2 = ok(call(h, agent, action="add", meta_task_id=meta_id, title="abandon me"))["subtask_id"]
    ok(call(h, agent, action="start", subtask_id=t1))
    ok(call(h, agent, action="fail", subtask_id=t1, summary="boom", failure_code="B"))
    ok(call(h, agent, action="start", subtask_id=t2))
    ok(call(h, agent, action="abandon", subtask_id=t2))
    shown = ok(call(h, reader, action="show", meta_task_id=meta_id))
    # Failure counts are explicit and no task is reported as completed.
    assert shown["counts"] == {"failed": 1, "abandoned": 1}
    # Covey closes the worklist at Covey's exact state name; lists marks it
    # closed, never successful/verified/landed.
    assert shown["meta_task"]["state"] == "completed"
    listed = next(i for i in ok(call(h, reader, action="lists"))["items"]
                  if i["meta_task_id"] == meta_id)
    assert listed["state"] == "completed" and listed["label"] == "closed"
    # A closed worklist is history: appending is refused and creates nothing.
    err(call(h, agent, action="add", meta_task_id=meta_id, title="too late"))
    assert len(ok(call(h, reader, action="show", meta_task_id=meta_id))["subtasks"]) == 2


def test_lease_expired_shown_before_reclaim(covey, covey_bin, agent_for, make_runtime):
    h = covey(db_path="/unused/placeholder.db")
    rt = make_runtime(h, agent_for("s1"), lease_duration_ms=400,
                      renew_interval_ms=10000, heartbeat_interval_ms=10000)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    started = ok(rt.execute({"action": "start", "subtask_id": tid}))
    stop_background(rt)  # maintenance stopped: persisted claim lags expiry
    sleep_until_ms(started["lease_deadline_ms"] + 300)
    shown = ok(rt.execute({"action": "show", "subtask_id": tid}))
    assert shown["subtask"]["state"] == "in_progress"  # no daemon reclaimed it
    assert shown["ownership_health"] == "lease_expired"


# ---------------------------------------------------------------------------
# AC7: failures
# ---------------------------------------------------------------------------

def test_same_request_replay_one_receipt_changed_content_conflicts(covey, tmp_path, agent_for):
    h = covey(db_path=str(tmp_path / "d.db"))
    agent = agent_for("s1")
    rid = str(uuid.uuid4())
    first = call(h, agent, action="create", prompt="once", request_id=rid)
    second = call(h, agent, action="create", prompt="once", request_id=rid)
    assert first["ok"] is True and second["ok"] is True
    assert first["data"]["meta_task_id"] == second["data"]["meta_task_id"]
    assert len(ok(call(h, agent, action="lists"))["items"]) == 1
    # Changed content under the same request id is a conflict, not a replay.
    changed = err(call(h, agent, action="create", prompt="different", request_id=rid))
    assert changed["code"] == "idempotency_conflict", changed
    assert len(ok(call(h, agent, action="lists"))["items"]) == 1
    # request_id must be a UUID: garbage is rejected, not silently minted.
    err(call(h, agent, action="create", prompt="x", request_id="req-fixed"))


def test_invalid_config_rejected_before_subprocess(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    mod = runtime_module(covey(db_path=str(tmp_path / "probe.db")))
    issued = []
    with intercepted(monkeypatch, mod, recording(issued)):
        # Relative path, empty path and wrong-typed binary never reach a runtime,
        # let alone a subprocess; the host answers a typed invalid_config.
        h2 = covey(binary=covey_bin, db_path="relative.db")
        env2 = err(call(h2, agent_for("s2"), action="create", prompt="x"))
        assert env2["code"] == "invalid_config", env2
        h3 = covey(binary=covey_bin, db_path="")
        env3 = err(call(h3, agent_for("s3"), action="lists"))
        assert env3["code"] == "invalid_config", env3
        h4 = covey(binary=123, db_path=str(tmp_path / "d.db"))
        env4 = err(call(h4, agent_for("s4"), action="status"))
        assert env4["code"] == "invalid_config", env4
        assert issued == []
    assert not (tmp_path / "relative.db").exists()
    assert not (tmp_path / "d.db").exists()
    # A missing binary is surfaced truthfully (capability probe fails), never a
    # fallback success, and the target DB is not created.
    h = covey(binary=str(tmp_path / "nope"), db_path=str(tmp_path / "missing-bin.db"))
    missing = err(call(h, agent_for("s1"), action="create", prompt="x"))
    assert missing["code"] == "unsupported_binary", missing
    assert not (tmp_path / "missing-bin.db").exists()


def test_malformed_output_on_claim_and_start_are_uncertain(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    mod = runtime_module(h)
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id, tid, orch = seed_worklist(covey_bin, db)
    second = seed_task(covey_bin, db, orch, meta_id, "second task")
    rid = str(uuid.uuid4())

    # 1) Claim response is malformed: uncertainty, request id preserved.
    def garbled_claim(real, args, **kwargs):
        if is_cmd(args, "subtask", "claim"):
            return {"ok": True, "data": "not-an-object"}
        return real(args, **kwargs)

    with intercepted(monkeypatch, mod, garbled_claim):
        claim_out = call(h, agent, action="start", subtask_id=tid, request_id=rid)
    err(claim_out)
    assert claim_out["code"] == "outcome_unknown", claim_out
    assert claim_out.get("request_id") == rid
    assert ok(call(h, reader, action="show", subtask_id=tid))["subtask"]["state"] == "available"
    # R3: the uncertain request stays replayable with the SAME request_id —
    # the retained idempotent claim re-issues exactly once; the request is
    # never wedged permanently as outcome_unknown.
    replay = call(h, agent, action="start", subtask_id=tid, request_id=rid)
    assert replay["ok"] is True, replay
    assert replay["data"]["state"] == "in_progress"

    # 2) Start response is malformed (claim succeeds, start envelope invalid).
    # The agent owns one task at a time, so give the first task back first.
    ok(call(h, agent, action="release", subtask_id=tid))

    def garbled_start(real, args, **kwargs):
        if is_cmd(args, "subtask", "start"):
            return {"ok": True, "data": ["nonsense"]}
        return real(args, **kwargs)

    rid2 = str(uuid.uuid4())
    with intercepted(monkeypatch, mod, garbled_start):
        partial = call(h, agent, action="start", subtask_id=second, request_id=rid2)
    err(partial)
    assert partial["code"] == "outcome_unknown", partial
    data = partial["data"]
    assert data["claim_state"] == "claimed" and data["started"] is False
    assert_no_tokens(json.dumps(partial), actual_tokens=db_tokens(db))
    # The claim that really exists is visible for reconciliation, never hidden
    # behind a fallback success.
    shown = ok(call(h, reader, action="show", subtask_id=second))
    assert shown["subtask"]["state"] == "claimed"
    assert shown["ownership_health"] == "held"
    # The same request resumes the half-done start on the retained claim.
    resumed = call(h, agent, action="start", subtask_id=second, request_id=rid2)
    assert resumed["ok"] is True and resumed["data"]["state"] == "in_progress", resumed


def test_committed_then_timeout_is_uncertain_single_effect(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    # `subtask finish` commits (task completed + receipt written), its result
    # is lost to a transport timeout. The explicit same-request replay reuses
    # the ORIGINAL idempotency key, succeeds once, and never duplicates the
    # receipt or the transition.
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    mod = runtime_module(h)
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    ok(call(h, agent, action="start", subtask_id=tid))
    rid = str(uuid.uuid4())
    finishes = []

    def timeout_after_commit(real, args, **kwargs):
        result = real(args, **kwargs)
        if is_cmd(args, "subtask", "finish"):
            finishes.append(list(args))
            if len(finishes) == 1:
                assert result.get("ok") is True  # the effect really committed...
                return dict(TIMEOUT_ENVELOPE)    # ...and its result was lost
        return result

    with intercepted(monkeypatch, mod, timeout_after_commit):
        out = call(h, agent, action="finish", subtask_id=tid, summary="committed",
                   request_id=rid)
        err(out)
        assert out["code"] == "outcome_unknown" and out.get("request_id") == rid
        # Truthful outcome: the effect exists exactly once before any replay.
        committed = ok(call(h, reader, action="show", subtask_id=tid))
        assert committed["subtask"]["state"] == "completed"
        assert [o["summary"] for o in committed["attempt_outcomes"]] == ["committed"]
        receipts_after_commit = db_query(db, "SELECT * FROM subtask_attempt_outcomes")
        finish_keys_after_commit = db_query(
            db, "SELECT idempotency_key FROM mutation_idempotency "
                "WHERE operation = 'finish_subtask'")
        assert len(receipts_after_commit) == 1 and len(finish_keys_after_commit) == 1
        finish_events_after_commit = db_query(
            db, "SELECT seq FROM event_log WHERE event_type = 'subtask_finished'")
        assert len(finish_events_after_commit) == 1
        replay = call(h, agent, action="finish", subtask_id=tid, summary="committed",
                      request_id=rid)
    assert replay["ok"] is True and replay["data"]["state"] == "completed", replay
    assert len(finishes) == 2
    assert flag_value(finishes[0], "--idempotency-key") == \
        flag_value(finishes[1], "--idempotency-key")  # the original key, not a fresh one
    final = ok(call(h, reader, action="show", subtask_id=tid))
    assert [o["summary"] for o in final["attempt_outcomes"]] == ["committed"]  # one receipt
    # No duplicate effect: the receipt row is the identical immutable record,
    # still one idempotency entry under the original key, and exactly one
    # completion transition. Unrelated lifecycle events (the executor
    # session-exit on confirmation) are deliberately not part of this invariant.
    assert db_query(db, "SELECT * FROM subtask_attempt_outcomes") == receipts_after_commit
    assert db_query(
        db, "SELECT idempotency_key FROM mutation_idempotency "
            "WHERE operation = 'finish_subtask'") == finish_keys_after_commit
    assert finish_keys_after_commit[0][0] == flag_value(finishes[0], "--idempotency-key")
    assert db_query(
        db, "SELECT seq FROM event_log WHERE event_type = 'subtask_finished'"
    ) == finish_events_after_commit


def test_register_timeout_reuses_same_principal_and_key(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    # Executor registration commits engine-side but its result is lost: the
    # explicit retry of the same request reuses the retained principal and
    # idempotency key, so the engine answers with the SAME session.
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    mod = runtime_module(h)
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    rid = str(uuid.uuid4())
    seen = []

    def commit_then_timeout_first_register(real, args, **kwargs):
        result = real(args, **kwargs)
        if is_cmd(args, "session", "register"):
            seen.append(list(args))
            if len(seen) == 1:
                assert result.get("ok") is True
                return dict(TIMEOUT_ENVELOPE)
        return result

    with intercepted(monkeypatch, mod, commit_then_timeout_first_register):
        first = call(h, agent, action="start", subtask_id=tid, request_id=rid)
        err(first)
        assert first["code"] == "outcome_unknown" and first.get("request_id") == rid
        assert ok(call(h, reader, action="show", subtask_id=tid))[
            "subtask"]["state"] == "available"  # nothing was claimed yet
        second = call(h, agent, action="start", subtask_id=tid, request_id=rid)
    # Same retained principal and same idempotency subkey on explicit retry;
    # no second principal is minted for the same request.
    assert len(seen) == 2, seen
    assert flag_value(seen[0], "--agent-principal-id") == flag_value(seen[1], "--agent-principal-id")
    assert flag_value(seen[0], "--idempotency-key") == flag_value(seen[1], "--idempotency-key")
    assert second["ok"] is True and second["data"]["state"] == "in_progress", second
    principal = flag_value(seen[0], "--agent-principal-id")
    assert principals(db, "executor") == [principal]  # one logical principal/session
    assert_no_tokens(json.dumps(first), actual_tokens=db_tokens(db))


def test_held_db_lock_gives_definite_error_under_5s(covey, tmp_path, agent_for):
    db = tmp_path / "locked.db"
    h = covey(db_path=str(db))
    agent = agent_for("s1")
    meta_id = ok(call(h, agent, action="create", prompt="lock me"))["meta_task_id"]
    holder = sqlite3.connect(str(db), timeout=0)
    holder.execute("PRAGMA busy_timeout=0")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE _blocker (x INTEGER)")
    t0 = time.monotonic()
    try:
        # WAL readers succeed under a held write lock, so the probe must be a
        # MUTATION: it needs the write lock and waits out Covey's busy timeout.
        result = call(h, agent, action="add", meta_task_id=meta_id, title="blocked")
    finally:
        holder.rollback()
        holder.close()
    elapsed = time.monotonic() - t0
    err(result)
    # A definite backend error, not an unknown/timeout outcome.
    assert result["code"] not in ("timeout", "outcome_unknown"), result
    assert 3 < elapsed < 15                 # Covey's real ~5s busy timeout
    # Definite error means no effect: the blocked task was never created.
    assert ok(call(h, agent, action="show", meta_task_id=meta_id))["subtasks"] == []


def test_transport_timeout_boundary_is_injected_not_lock_timing(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    # Transport timeout is a deterministic, injected boundary — never a real
    # SQLite lock duration assertion (Covey returns its definite lock error
    # after its ~5s busy timeout; see test_held_db_lock_gives_definite_error_under_5s).
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    mod = runtime_module(h)
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    rid = str(uuid.uuid4())
    claims = []

    def timeout_after_claim_commit(real, args, **kwargs):
        if is_cmd(args, "subtask", "claim"):
            claims.append(list(args))
            result = real(args, **kwargs)        # claim may have committed...
            if len(claims) == 1:
                return dict(TIMEOUT_ENVELOPE)    # ...result lost
            return result
        return real(args, **kwargs)

    with intercepted(monkeypatch, mod, timeout_after_claim_commit):
        out = call(h, agent, action="start", subtask_id=tid, request_id=rid)
        err(out)
        assert out["code"] == "outcome_unknown" and out.get("request_id") == rid, out
        # Uncertain claim: outcome unconfirmed; the same-key replay resolves it
        # through the engine's idempotency rather than duplicating the claim.
        replay = call(h, agent, action="start", subtask_id=tid, request_id=rid)
    assert replay["ok"] is True, replay
    assert replay["data"]["state"] == "in_progress"
    assert len(claims) == 2
    assert flag_value(claims[0], "--idempotency-key") == flag_value(claims[1], "--idempotency-key")
    shown = ok(call(h, reader, action="show", subtask_id=tid))
    assert shown["subtask"]["state"] == "in_progress"
    # One logical claim: the replay did not create a second one.
    assert db_query(db, "SELECT COUNT(*) FROM claims WHERE subtask_id = ?", (tid,)) == [(1,)]


def test_reaper_heartbeat_loss_retires_and_stops_renewing(covey, covey_bin, agent_for, make_runtime, monkeypatch):
    h = covey(db_path="/unused/placeholder.db")
    mod = runtime_module(h)
    rt = make_runtime(h, agent_for("s1"), lease_duration_ms=2000,
                      renew_interval_ms=100, heartbeat_interval_ms=100)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    calls = []

    def reaped(real, args, **kwargs):
        calls.append(list(args))
        if is_cmd(args, "session", "heartbeat"):
            # External reaper killed the session under our feet.
            return {"ok": False, "code": "session_not_active",
                    "message": "SessionNotActive: session session_deadbeef was reaped"}
        return real(args, **kwargs)

    with intercepted(monkeypatch, mod, reaped):
        ok(rt.execute({"action": "start", "subtask_id": tid}))
        stop_background(rt)
        tick_when_due(rt)  # heartbeat refused -> definite loss -> authority retired
        marks = len(calls)
        for _ in range(3):
            rt.maintenance_once()  # retired: nothing further goes out
        assert len(calls) == marks
    # The failed heartbeat was not followed by a renewal, and none went out later.
    assert not any(is_cmd(c, "claim", "renew") for c in calls)
    assert sum(1 for c in calls if is_cmd(c, "session", "heartbeat")) >= 1
    # The retired failure is visible & redacted; the old authority is not replayed.
    status = ok(rt.execute({"action": "status"}))
    assert status["active_claim"] is None
    assert status["lost_reason"] is not None
    assert status["maintenance_running"] is False
    assert "session_deadbeef" not in json.dumps(status)
    assert renew_keys(rt.db_file) == []


def test_retired_token_replay_refused_new_request_works(covey, covey_bin, agent_for, make_runtime, monkeypatch):
    h = covey(db_path="/unused/placeholder.db")
    mod = runtime_module(h)
    rt = make_runtime(h, agent_for("s1"), lease_duration_ms=2000,
                      renew_interval_ms=100, heartbeat_interval_ms=100)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    rid = str(uuid.uuid4())

    def reaped(real, args, **kwargs):
        if is_cmd(args, "session", "heartbeat"):
            return {"ok": False, "code": "session_not_active", "message": "reaped"}
        return real(args, **kwargs)

    with intercepted(monkeypatch, mod, reaped):
        first = ok(rt.execute({"action": "start", "subtask_id": tid, "request_id": rid}))
        stop_background(rt)
        tick_when_due(rt)  # retire the executor token (heartbeat reaped)
        assert ok(rt.execute({"action": "status"}))["active_claim"] is None
        replay = rt.execute({"action": "start", "subtask_id": tid, "request_id": rid})
    assert replay["ok"] is False and replay["code"] == "replay_refused", replay
    assert replay["request_id"] == rid
    # A NEW explicit action is not a transparent replay: it honestly re-claims
    # under a fresh principal (the retired one's pending exit releases the old claim).
    again = ok(rt.execute({"action": "start", "subtask_id": tid}))
    assert again["claim_id"] != first["claim_id"]
    assert again["fence_seq"] > first["fence_seq"]
    assert len(set(principals(rt.db_file, "executor"))) == 2


def test_partial_claim_start_is_truthful_partial_state(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    mod = runtime_module(h)
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id, tid, _ = seed_worklist(covey_bin, db)

    def fail_start(real, args, **kwargs):
        if is_cmd(args, "subtask", "start"):
            return {"ok": False, "code": "invalid_transition",
                    "message": "claimed -> in_progress denied (simulated)"}
        return real(args, **kwargs)

    with intercepted(monkeypatch, mod, fail_start):
        result = call(h, agent, action="start", subtask_id=tid)
    assert result["ok"] is False
    data = result.get("data") or {}
    assert data["claim_state"] == "claimed" and data["started"] is False
    assert result["code"] == "invalid_transition"  # engine truth, not fake error
    shown = ok(call(h, reader, action="show", subtask_id=tid))
    assert shown["subtask"]["state"] == "claimed"  # the owner agent is still alive
    assert shown["ownership_health"] == "held"
    # The partial state is recoverable: the owner releases the claimed task.
    rel = call(h, agent, action="release", subtask_id=tid)
    assert rel["ok"] is True and rel["data"]["state"] == "available", rel
    assert ok(call(h, reader, action="show", subtask_id=tid))["subtask"]["state"] == "available"


# ---------------------------------------------------------------------------
# AC8: isolation and secrets
# ---------------------------------------------------------------------------

def test_own_and_foreign_tokens_never_in_output_or_logs(covey, covey_bin, tmp_path, agent_for, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    db = tmp_path / "tok.db"
    h = covey(db_path=str(db))
    owner, viewer, intruder = agent_for("owner"), agent_for("viewer"), agent_for("intruder")
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    ok(call(h, owner, action="start", subtask_id=tid))
    foreign = db_tokens(db)
    assert foreign  # real tokens exist in the DB (own + seed foreign)
    outputs = [
        call(h, owner, action="status"),
        call(h, owner, action="lists"),
        call(h, viewer, action="show", subtask_id=tid),
        call(h, viewer, action="show", meta_task_id=meta_id),
        call(h, intruder, action="finish", subtask_id=tid, summary="x"),
        call(h, intruder, action="release", subtask_id=tid),
        call(h, intruder, action="start", subtask_id=tid),  # conflict with the owner's claim
    ]
    for payload in outputs:
        assert_no_tokens(json.dumps(payload), actual_tokens=foreign)
    # Synthetic malformed diagnostics must also be redacted.
    mod = runtime_module(h)

    def tainted(real, args, **kwargs):
        if is_cmd(args, "meta", "list"):
            return {"ok": False, "code": "internal_error",
                    "message": "backend rejected session_deadbeefcafe token session_0123"}
        return real(args, **kwargs)

    with intercepted(monkeypatch, mod, tainted):
        out = call(h, owner, action="lists")
    err(out)
    assert "session_deadbeefcafe" not in json.dumps(out), out
    assert_no_tokens(json.dumps(out))
    # Logs carry no actual token either.
    for token in db_tokens(db):
        assert token not in caplog.text


def test_donor_binary_and_db_env_ignored(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    donor_db = tmp_path / "donor.db"
    monkeypatch.setenv("COVEY_DB_PATH", str(donor_db))
    monkeypatch.setenv("COVEY_BIN", str(tmp_path / "donor-covey"))
    db = tmp_path / "configured.db"
    h = covey(db_path=str(db))
    agent = agent_for("agent")
    ok(call(h, agent, action="create", prompt="configured"))
    assert len(ok(call(h, agent, action="lists"))["items"]) == 1
    assert db.exists()
    assert not donor_db.exists()  # donor env DB never opened
    # Donor COVEY_BIN ignored: operations used the configured binary and
    # succeeded (a donor fake binary would have failed everything).
    st = ok(call(h, agent, action="status"))
    assert st["binary"] == covey_bin and st["db_path"] == str(db), st
    assert "donor-covey" not in json.dumps(st)


def test_concurrent_profile_scopes_keep_distinct_configs_and_databases(covey, covey_bin, tmp_path, monkeypatch):
    # One registration serves two profiles at once through the SDK's native
    # context-local home override (never os.environ, which every thread shares).
    # Each profile's own config.yaml decides its binary/db_path.
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    monkeypatch.setenv("COVEY_DB_PATH", str(tmp_path / "donor.db"))
    monkeypatch.setenv("COVEY_BIN", str(tmp_path / "donor-covey"))
    default_db = tmp_path / "default.db"
    h = covey(db_path=str(default_db))
    profiles = {}
    for name in ("pa", "pb"):
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        db = tmp_path / f"{name}.db"
        (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
            "enabled": ["covey-tasks"],
            "entries": {"covey-tasks": {
                "enabled": True,
                "settings": {"binary": covey_bin, "db_path": str(db)}}}}}))
        profiles[name] = (home, db)
    barrier = threading.Barrier(2)
    results, failures = {}, []

    def worker(name):
        home, _ = profiles[name]
        token = set_hermes_home_override(home)  # context-local to THIS thread
        try:
            agent = new_agent(f"agent-{name}")  # held strongly for the whole worker
            barrier.wait(30)
            meta_id = ok(call(h, agent, action="create",
                              prompt=f"worklist {name}"))["meta_task_id"]
            tid = ok(call(h, agent, action="add", meta_task_id=meta_id,
                          title=f"task {name}"))["subtask_id"]
            ok(call(h, agent, action="start", subtask_id=tid))
            shown = ok(call(h, agent, action="show", meta_task_id=meta_id))
            listed = ok(call(h, agent, action="lists"))["items"]
            status = ok(call(h, agent, action="status"))
            ok(call(h, agent, action="finish", subtask_id=tid, summary=f"done {name}"))
            results[name] = {"meta_id": meta_id, "tid": tid, "titles": [
                t["title"] for t in shown["subtasks"]], "prompts": [
                i["prompt_text"] for i in listed], "status": status}
        except BaseException as exc:  # surfaced below, not a stray thread warning
            failures.append((name, repr(exc)))
        finally:
            reset_hermes_home_override(token)

    threads = [threading.Thread(target=worker, args=(name,)) for name in profiles]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not any(t.is_alive() for t in threads)
    assert not failures, failures
    for name, (home, db) in profiles.items():
        # Each profile saw exactly its own worklist and task, from its own DB.
        assert results[name]["prompts"] == [f"worklist {name}"], results[name]
        assert results[name]["titles"] == [f"task {name}"], results[name]
        assert results[name]["status"]["db_path"] == str(db)
        assert results[name]["status"]["binary"] == covey_bin
        rows = fresh_covey(covey_bin, db, "meta", "list")
        assert [row["prompt_text"] for row in rows] == [f"worklist {name}"]
        assert [row["meta_task_id"] for row in rows] == [results[name]["meta_id"]]
        task = fresh_covey(covey_bin, db, "subtask", "status",
                           "--subtask-id", results[name]["tid"])
        assert task["subtask"]["state"] == "completed"
    assert results["pa"]["meta_id"] != results["pb"]["meta_id"]
    # Neither the default profile's DB nor the donor env DB was ever opened.
    assert not default_db.exists()
    assert not (tmp_path / "donor.db").exists()


def test_ownership_fields_and_model_paths_rejected_before_subprocess(covey, tmp_path, agent_for, monkeypatch):
    h = covey(db_path=str(tmp_path / "d.db"))
    issued = []
    mod = runtime_module(h)
    agent = agent_for("s1")
    with intercepted(monkeypatch, mod, recording(issued)):
        for extra in ({"session_token": "session_abc"},
                      {"owner_session_token": "session_abc"},
                      {"db_path": str(tmp_path / "evil.db")},
                      {"binary": str(tmp_path / "evil-covey")},
                      {"meta_task_id": "m", "subtask_id": "s"}):
            args = {"action": "create", "prompt": "x"}
            args.update(extra)
            env = call(h, agent, **args)
            err(env)
            assert env["code"] == "invalid_arguments", env
        # Wrong-typed values rejected the same way (bool-as-int included).
        err(call(h, agent, action="add", meta_task_id="m", title="t", priority="50"))
        err(call(h, agent, action="lists", limit=True))
        err(call(h, agent, action="show"))
        err(call(h, agent, action="finish", subtask_id="s", summary="ok",
                 evidence_digest=True))
        # Priority must be a plain int inside the documented 0..1000 range;
        # out-of-range and bool-as-int values are rejected before transport.
        for bad in (-1, -9223372036854775808, 1001, 9223372036854775807, True, False):
            env = call(h, agent, action="add", meta_task_id="m",
                       title="t", priority=bad)
            err(env)
            assert env["code"] == "invalid_arguments", env
        # Embedded NUL cannot cross argv: rejected as invalid_arguments
        # before the parser ever builds a command (argv has no NUL).
        err(call(h, agent, action="create", prompt="nul\x00byte"))
        err(call(h, agent, action="add", meta_task_id="m", title="nul\x00title"))
        err(call(h, agent, action="finish", subtask_id="s", summary="nul\x00summary"))
    assert not issued  # every rejection short-circuited before any Covey call
    # No rejected call reached the configured tmp DB (zero transport calls
    # above already prove it): isolation is asserted on tmp_path-owned paths
    # only, never on global /tmp state.
    assert not (tmp_path / "d.db").exists() and not (tmp_path / "evil.db").exists()


def test_shell_metacharacters_round_trip_literally(covey, tmp_path, agent_for):
    h = covey(db_path=str(tmp_path / "meta.db"))
    agent = agent_for("s1")
    text = "rm -rf / && echo $(whoami)"
    meta_id = ok(call(h, agent, action="create", prompt=text))["meta_task_id"]
    tid = ok(call(h, agent, action="add", meta_task_id=meta_id, title=text))["subtask_id"]
    shown = ok(call(h, agent, action="show", meta_task_id=meta_id))
    # The observable contract is byte-for-byte round-trip through the real
    # transport (shell=False): shell metacharacters were never parsed and
    # the exact bytes persisted unchanged.
    assert shown["meta_task"]["prompt_text"] == text
    assert shown["subtasks"][0]["title"] == text
    assert shown["subtasks"][0]["subtask_id"] == tid
    assert not list(tmp_path.glob("*whoami*"))  # no side effects executed


def test_literal_dash_and_boundary_priorities_round_trip_via_real_cli(
        covey, covey_bin, tmp_path, agent_for):
    # The runtime boundary must pass dash-leading text literally and accept
    # the documented priority boundaries (Covey SubtaskPriority 0..1000):
    # with the option and value reaching clap as separate tokens, a value
    # starting with "-" is re-parsed as an option and the create fails with
    # `unexpected argument '- '`, before anything is written. The fixed
    # boundary encodes each value attached to its option and the bytes
    # round-trip unchanged through the real Covey store.
    db = tmp_path / "literal.db"
    h = covey(db_path=str(db))
    agent, reader = agent_for("s1"), agent_for("s2")
    prompt = "- literal prompt"
    title = "- literal task"
    uni_title = "- café ; $(x) `y` | & > *"
    meta_meta_id = ok(call(h, agent, action="create", prompt=prompt))["meta_task_id"]
    # Seed every task up front, before any lifecycle action below.
    t_zero = ok(call(h, agent, action="add", meta_task_id=meta_meta_id,
                     title=title, priority=0))["subtask_id"]
    t_max = ok(call(h, agent, action="add", meta_task_id=meta_meta_id,
                    title=title, priority=1000))["subtask_id"]
    t_uni = ok(call(h, agent, action="add", meta_task_id=meta_meta_id,
                    title=uni_title))["subtask_id"]
    shown = ok(call(h, reader, action="show", meta_task_id=meta_meta_id))
    assert shown["meta_task"]["prompt_text"] == prompt
    fields = {t["subtask_id"]: t for t in shown["subtasks"]}
    assert fields[t_zero]["title"] == title
    assert fields[t_max]["title"] == title
    assert fields[t_uni]["title"] == uni_title
    assert fields[t_zero]["priority"] == 0
    assert fields[t_max]["priority"] == 1000
    # finish/retry/fail text persists literally, metacharacters included.
    retry_text = "- flake ; $(touch evil) `id` | & > *"
    ok(call(h, agent, action="start", subtask_id=t_zero))
    ok(call(h, agent, action="retry", subtask_id=t_zero,
            summary=retry_text, failure_code="-FLAKE"))
    ok(call(h, agent, action="start", subtask_id=t_zero))
    fail_text = "- broke ; $(whoami) | & > *"
    ok(call(h, agent, action="fail", subtask_id=t_zero,
            summary=fail_text, failure_code="-BAD"))
    fin_text = "- done ; $(x) `y` | & > *"
    ok(call(h, agent, action="start", subtask_id=t_max))
    ok(call(h, agent, action="finish", subtask_id=t_max, summary=fin_text))
    rows = db_query(db, "SELECT summary FROM subtask_attempt_outcomes ORDER BY rowid")
    assert [r[0] for r in rows] == [retry_text, fail_text, fin_text]
    # No extra option effects: the metacharacter payloads were never executed
    # (files named after their contents would exist) and the state machine
    # advanced exactly along retry -> failed for one and completed for the
    # other; the dash-leading prompt/task survived unchanged.
    assert not list(tmp_path.glob("*evil*"))
    final = ok(call(h, reader, action="show", meta_task_id=meta_meta_id))
    states = {t["subtask_id"]: t["state"] for t in final["subtasks"]}
    assert states[t_zero] == "failed" and states[t_max] == "completed"
    assert states[t_uni] == "available"
    assert final["meta_task"]["prompt_text"] == prompt
    titles = {t["subtask_id"]: t["title"] for t in final["subtasks"]}
    assert titles[t_zero] == title and titles[t_uni] == uni_title




# ---------------------------------------------------------------------------
# AC10: capability and stale binary
# ---------------------------------------------------------------------------

def test_status_reports_capabilities_never_opens_db_or_registers(covey, tmp_path, agent_for):
    db = tmp_path / "cap.db"
    h = covey(db_path=str(db))
    status = call(h, agent_for("s1"), action="status")
    assert status["ok"] is True, status  # diagnostics stay discoverable
    caps = status["data"]["capabilities"]
    # Capabilities were actually probed against the binary (never merely a
    # version string), and every required one is present.
    assert caps and all(value is True for value in caps.values()), caps
    assert status["data"]["orchestrator_registered"] is False
    assert status["data"]["executor_registered"] is False
    assert not db.exists()
    assert not (tmp_path / "cap.db-wal").exists()
    assert not (tmp_path / "cap.db-shm").exists()


def test_stale_binary_refuses_task_operations_with_guidance(covey, tmp_path, agent_for):
    # Same version string, no new capabilities: capability probing (not
    # version) must refuse operations and supply build guidance.
    fake = tmp_path / "covey-stale"
    fake.write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do\n"
        "  [ \"$a\" = \"--help\" ] && "
        "{ echo '{\"ok\":true,\"data\":{\"command\":\"covey\",\"help_text\":\"stale covey usage\"}}'; exit 0; }\n"
        "done\n"
        "echo '{\"ok\":false,\"code\":\"invalid_args\",\"message\":\"unrecognized subcommand\"}'\n"
        "exit 1\n")
    fake.chmod(0o755)
    db = tmp_path / "stale.db"
    h = covey(binary=str(fake), db_path=str(db))
    op = call(h, agent_for("s1"), action="create", prompt="x")
    err(op)
    assert op["code"] == "unsupported_binary", op
    assert op.get("suggestions"), op
    assert not db.exists()  # probes must not open the target DB


def test_on_unload_closes_runtime_and_releases_claim(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    agent = agent_for("s1")
    ok(call(h, agent, action="start", subtask_id=tid))
    h.manager.unload("covey-tasks")
    shown = fresh_covey(covey_bin, str(db), "subtask", "status",
                        "--subtask-id", tid)
    assert shown["subtask"]["state"] == "available"  # claim released on unload
    assert shown["claim"] is None


# ---------------------------------------------------------------------------
# R3 lifecycle regressions (independent review findings)
# ---------------------------------------------------------------------------

def test_concurrent_close_bounded_and_no_late_authority(covey, covey_bin, agent_for, make_runtime, monkeypatch):
    # close() must revoke local authority and return inside its deadline even
    # while an action is in flight in a 30s transport call; the late in-flight
    # result may never publish fresh local authority.
    h = covey(db_path="/unused/placeholder.db")
    mod = runtime_module(h)
    rt = make_runtime(h, agent_for("s1"), lease_duration_ms=300000,
                      renew_interval_ms=100, heartbeat_interval_ms=100,
                      teardown_timeout=2.0)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    in_flight, release = threading.Event(), threading.Event()
    outcome = {}

    def blocking(real, args, **kwargs):
        if is_cmd(args, "subtask", "claim"):
            in_flight.set()      # transport genuinely in flight...
            release.wait(20)     # ...until the test lets it return
        return real(args, **kwargs)

    def worker():
        outcome["result"] = rt.execute({"action": "start", "subtask_id": tid})

    with intercepted(monkeypatch, mod, blocking):
        th = threading.Thread(target=worker)
        th.start()
        try:
            assert in_flight.wait(15), "start never reached the claim transport call"
            t0 = time.monotonic()
            rt.close()
            elapsed = time.monotonic() - t0
        finally:
            release.set()  # release the in-flight command
            th.join(30)
    assert not th.is_alive()
    assert elapsed <= 4.0, f"close exceeded deadline+slack: {elapsed:.1f}s"
    assert rt.closed is True
    # No late claim publication: the late in-flight claim success was refused
    # as `closed`, no maintenance thread was started for it, and the runtime
    # answers `closed` from here on (engine lease expiry is the backstop).
    assert outcome["result"]["ok"] is False and outcome["result"]["code"] == "closed", outcome
    assert rt._thread is None
    assert rt._stop_event.is_set()
    assert rt.execute({"action": "status"})["code"] == "closed"
    assert rt.execute({"action": "start", "subtask_id": tid})["code"] == "closed"


def test_teardown_budget_exhaustion_releases_cwd_routing(
        covey, covey_bin, tmp_path, agent_for, make_runtime, monkeypatch):
    # Bounded close while the runtime lock is held: the native claim
    # truthfully stays with lease expiry (uncertain outcome — never
    # fabricate a release), but cwd routing and the worktree flock are
    # revoked NOW.  A stale cwd would route ordinary relative writes into
    # the retired worktree (engine fences do not govern plain filesystem
    # writes) and no late path may re-bind it.
    from tools import terminal_tool
    from model_tools import handle_function_call
    from tools.terminal_tool import get_session_cwd, record_session_cwd

    h = covey(db_path="/unused/placeholder.db")
    mod = runtime_module(h)
    agent = agent_for("teardown-budget")
    rt = make_runtime(h, agent, lease_duration_ms=300000,
                      renew_interval_ms=100, heartbeat_interval_ms=100,
                      teardown_timeout=2.0)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    _, workspace = linked_worktree(tmp_path)
    prior = tmp_path / "prior-cwd"
    prior.mkdir()
    record_session_cwd(agent.session_id, str(prior))

    ok(rt.execute({"action": "start", "subtask_id": tid,
                   "workspace": str(workspace)}))
    stop_background(rt)  # no renewal races against the assertion window
    assert get_session_cwd(agent.session_id) == str(workspace)

    # Park a second `_mutation` inside the runtime lock via the transport
    # seam; teardown then exhausts its budget on lock acquisition.
    in_flight, release = threading.Event(), threading.Event()
    outcome = {}

    def blocking(real, args, **kwargs):
        if is_cmd(args, "subtask", "status"):
            in_flight.set()
            release.wait(20)
        return real(args, **kwargs)

    def worker():
        outcome["result"] = rt.execute({"action": "start", "subtask_id": tid})

    with intercepted(monkeypatch, mod, blocking):
        th = threading.Thread(target=worker)
        th.start()
        try:
            assert in_flight.wait(15), "in-flight status call never parked"
            # Deterministic overlap around native cwd publication: park a
            # guard pass at its record_session_cwd publication step and run
            # close concurrently.  Pre-fix (no serialization) close restores
            # prior cwd and returns while the parked publication then lands
            # the retired workspace; with the fix close's revoke waits for
            # the in-flight publication to land, then revokes it.
            entered, resume = threading.Event(), threading.Event()
            fired = {"once": False}
            orig_record = terminal_tool.record_session_cwd

            def pausing_record(key, value):
                if value == str(workspace) and not fired["once"]:
                    fired["once"] = True
                    entered.set()
                    resume.wait(20)
                return orig_record(key, value)

            def guard_worker():
                guard_worker.outcome = rt.workspace_guard(
                    "terminal", {"command": "ls"})

            gw = threading.Thread(target=guard_worker)
            close_worker = threading.Thread(target=rt.close)
            with monkeypatch.context() as m:
                m.setattr(terminal_tool, "record_session_cwd", pausing_record)
                gw.start()
                assert entered.wait(15), "guard publication never parked"
                close_worker.start()
                time.sleep(2.6)
                resume.set()
                close_worker.join(30)
                gw.join(30)
            assert not close_worker.is_alive()
            assert not gw.is_alive()

            # Budget exhausted while the claim stays uncertain: routing
            # released, flock free, guard closed for good.
            # No elapsed bound here: close's own deadline starts after the
            # serialized publish revoke; its budgeted bound for a held
            # runtime lock is asserted by
            # test_concurrent_close_bounded_and_no_late_authority.
            assert rt.closed is True
            assert get_session_cwd(agent.session_id) == str(prior), \
                "budget-exhausted close must restore cwd, not leave the retired workspace routing"
            assert rt.workspace_guard("terminal", {"command": "ls"}) is None, \
                "closed runtime must not guard or rebind"
            peer_rt = mod.CoveyRuntime(agent_for("tdb-peer"), binary=covey_bin,
                                       db_path=str(rt.db_file),
                                       profile_home=str(tmp_path))
            try:
                assert peer_rt._acquire_workspace_lock(str(workspace)) is None, \
                    "budget-exhausted close must release the worktree flock"
            finally:
                peer_rt.close()

            # Real consumer: a native relative write resolves through the
            # restored cwd and cannot land in the retired worktree.
            rel = "after-close.py"
            handle_function_call(
                "write_file", {"path": rel, "content": "restored\n"},
                session_id=agent.session_id,
                task_id=getattr(agent, "_current_task_id", agent.session_id),
                tool_call_id=uuid.uuid4().hex)
            assert (prior / rel).read_text() == "restored\n"
            assert not (workspace / rel).exists()
        finally:
            release.set()  # release the parked backend call
            th.join(30)

    # Late authority cannot re-bind: the parked request returned without
    # publishing, and the guard stays closed after it lands.
    assert not th.is_alive()
    assert rt.workspace_guard("terminal", {"command": "ls"}) is None
    assert get_session_cwd(agent.session_id) == str(prior)
    # The claim was never fabricated-released: the native engine still
    # carries this claim truthfully (engine lease expiry is the backstop).
    task = fresh_covey(covey_bin, rt.db_file, "subtask", "status",
                       "--subtask-id", tid)
    assert task["claim"] is not None
    assert task["subtask"]["state"] == "in_progress"
    assert outcome["result"]["ok"] is False  # refused, not applied


def test_claim_publication_atomic_against_close_revocation(
        covey, covey_bin, tmp_path, agent_for, make_runtime, monkeypatch):
    # The claim-application thread holds the runtime lock and passes the
    # _closed check, then pauses at the _Claim construction seam; close
    # revokes and its budget branch runs while the claim is still parked.
    # Publication itself is atomic under the publish mutex, so after close
    # the claim is either refused outright or (when publication landed
    # first) close's revocation restores the authoritative cwd — the
    # retired workspace can never remain the agent's cwd either way.
    from tools.terminal_tool import get_session_cwd, record_session_cwd

    h = covey(db_path="/unused/placeholder.db")
    mod = runtime_module(h)
    agent = agent_for("late-pub")
    rt = make_runtime(h, agent, lease_duration_ms=300000,
                      renew_interval_ms=100, heartbeat_interval_ms=100,
                      teardown_timeout=2.0)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    _, workspace = linked_worktree(tmp_path)
    prior = tmp_path / "prior-cwd"
    prior.mkdir()
    record_session_cwd(agent.session_id, str(prior))

    entered, resume = threading.Event(), threading.Event()
    orig_claim_cls = mod._Claim

    def pausing_claim(*args, **kwargs):
        entered.set()
        resume.wait(20)
        return orig_claim_cls(*args, **kwargs)

    def worker():
        worker.result = rt.execute({"action": "start", "subtask_id": tid,
                                    "workspace": str(workspace)})

    with monkeypatch.context() as m:
        m.setattr(mod, "_Claim", pausing_claim)
        th = threading.Thread(target=worker)
        th.start()
        try:
            assert entered.wait(20), "_Claim construction seam never reached"
            close_worker = threading.Thread(target=rt.close)
            close_worker.start()
            # Deliberate test parking (NOT a subprocess wait): with the fix
            # close blocks here until the in-flight publication lands, and
            # the budget fixed BEFORE the publish guard then exhausts on
            # the runtime lock.  Pre-fix close returns immediately while
            # the claim is still parked and publishes into a revoked
            # runtime once resumed.
            time.sleep(2.6)
            resume.set()
            close_worker.join(30)
            th.join(30)
        finally:
            resume.set()
    assert not close_worker.is_alive()
    assert not th.is_alive()
    assert rt.closed is True
    # The late claim result is refused, never applied as fresh authority.
    assert worker.result["ok"] is False
    # Whichever serialization order the fix chose, the retired workspace
    # cannot remain routed: cwd is the restored authoritative prior.
    assert get_session_cwd(agent.session_id) == str(prior), \
        "claim publication raced close revocation and left the workspace as cwd"
    # Guard state on a closed runtime is inert (the plugin registry evicted
    # it) and conservative either way: workspace_guard_closed refuses
    # mutations if force-called, and the retirement path that re-opened it
    # never re-binds cwd.  The authoritative invariant is the cwd itself.
    directive = rt.workspace_guard("terminal", {"command": "ls"})
    assert directive is None or directive.get("code") == "workspace_guard_closed", directive
    # The guard call itself must not (re)bind cwd either.
    assert get_session_cwd(agent.session_id) == str(prior)

    # The native claim truthfully stays with the engine (no fabricated
    # release, no locally applied authority in a closed runtime).
    task = fresh_covey(covey_bin, rt.db_file, "subtask", "status",
                       "--subtask-id", tid)
    assert task["claim"] is not None


def test_uncertain_register_retry_reuses_principal_and_token(covey, tmp_path, agent_for, monkeypatch):
    # Review defect: uncertain requests must not wedge into permanent
    # outcome_unknown. After an orchestrator register timeout (the request never
    # reached the engine) the same request_id replay reuses the retained
    # principal+key and succeeds with one logical principal.
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    mod = runtime_module(h)
    agent = agent_for("s1")
    rid = str(uuid.uuid4())
    seen = []

    def timeout_register_once(real, args, **kwargs):
        if is_cmd(args, "session", "register"):
            seen.append(list(args))
            if len(seen) == 1:
                return dict(TIMEOUT_ENVELOPE)
        return real(args, **kwargs)

    with intercepted(monkeypatch, mod, timeout_register_once):
        first = call(h, agent, action="create", prompt="uncertain register",
                     request_id=rid)
        err(first)
        assert first["code"] == "outcome_unknown", first
        second = call(h, agent, action="create", prompt="uncertain register",
                      request_id=rid)
    assert second["ok"] is True, second  # replay, never a permanent wedge
    assert len(seen) == 2, seen
    assert flag_value(seen[0], "--agent-principal-id") == flag_value(seen[1], "--agent-principal-id")
    assert flag_value(seen[0], "--idempotency-key") == flag_value(seen[1], "--idempotency-key")
    assert principals(db, "orchestrator") == [flag_value(seen[0], "--agent-principal-id")]
    items = ok(call(h, agent, action="lists"))["items"]
    assert len(items) == 1  # one logical worklist, no duplicate create


def test_committed_after_timeout_reconcile_replay_one_effect(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    # meta submit commits, the response is lost, and the same request_id
    # replay reconciles through the engine's idempotency: exactly one
    # worklist, no blind duplicate, and no permanent wedge.
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    mod = runtime_module(h)
    agent = agent_for("s1")
    rid = str(uuid.uuid4())
    submits = []

    def lost_submit(real, args, **kwargs):
        result = real(args, **kwargs)              # effect commits...
        if is_cmd(args, "meta", "submit"):
            submits.append(list(args))
            if len(submits) == 1:
                assert result.get("ok") is True
                return dict(TIMEOUT_ENVELOPE)      # ...result lost
        return result

    with intercepted(monkeypatch, mod, lost_submit):
        first = call(h, agent, action="create", prompt="committed submit",
                     request_id=rid)
        err(first)
        assert first["code"] == "outcome_unknown", first
        # Reconcile path: list first, then replay the same request.
        listed = ok(call(h, agent, action="lists"))["items"]
        assert len(listed) == 1
        events_after_commit = covey_events(covey_bin, db)
        second = call(h, agent, action="create", prompt="committed submit",
                      request_id=rid)
    assert second["ok"] is True, second
    assert second["data"]["meta_task_id"] == listed[0]["meta_task_id"]  # the stored result
    assert flag_value(submits[0], "--idempotency-key") == flag_value(submits[1], "--idempotency-key")
    assert len(ok(call(h, agent, action="lists"))["items"]) == 1
    assert covey_events(covey_bin, db) == events_after_commit  # replay added no effect


def test_partial_start_precommit_failure_resumes_on_retry(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    # Claim succeeds, `subtask start` fails before committing. The failed
    # attempt must report the partial state truthfully, and a retry with the
    # SAME request_id must re-issue `subtask start` on the retained live
    # claim (not silently hold a 'claimed' claim forever).
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    mod = runtime_module(h)
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    fails = {"count": 0}

    def fail_start_once(real, args, **kwargs):
        if is_cmd(args, "subtask", "start"):
            fails["count"] += 1
            if fails["count"] == 1:
                return {"ok": False, "code": "invalid_transition",
                        "message": "claimed -> in_progress denied (simulated)"}
        return real(args, **kwargs)

    rid = str(uuid.uuid4())
    with intercepted(monkeypatch, mod, fail_start_once):
        first = call(h, agent, action="start", subtask_id=tid, request_id=rid)
        assert first["ok"] is False, first
        assert first["code"] == "invalid_transition", first  # engine truth
        data = first.get("data") or {}
        assert data.get("claim_id") and data.get("fence_seq") is not None
        assert data.get("started") is False
        shown = ok(call(h, reader, action="show", subtask_id=tid))
        assert shown["subtask"]["state"] == "claimed"  # partial state persisted
        retry = call(h, agent, action="start", subtask_id=tid, request_id=rid)
    assert retry["ok"] is True, retry  # resume the half-done start
    assert retry["data"]["state"] == "in_progress"
    assert retry["data"]["claim_id"] == data["claim_id"]  # same claim, not a second one
    assert fails["count"] == 2  # start was actually re-issued


def test_partial_start_committed_timeout_reports_truthful_state(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    # `subtask start` commits but the result is lost: the envelope is
    # outcome_unknown and any state it reports must be the persisted truth
    # (in_progress), never a hard-coded 'claimed'. The same-request retry
    # resolves to in_progress without issuing a second start.
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    mod = runtime_module(h)
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    starts = []

    def lost_start(real, args, **kwargs):
        result = real(args, **kwargs)              # start commits...
        if is_cmd(args, "subtask", "start"):
            starts.append(list(args))
            return dict(TIMEOUT_ENVELOPE)          # ...result lost
        return result

    rid = str(uuid.uuid4())
    with intercepted(monkeypatch, mod, lost_start):
        first = call(h, agent, action="start", subtask_id=tid, request_id=rid)
        err(first)
        assert first["code"] == "outcome_unknown", first
        data = first["data"]
        # Truthful only: the persisted truth.
        assert data["state"] == "in_progress" and data["claim_state"] == "in_progress"
        assert data["started"] is True
        retry = call(h, agent, action="start", subtask_id=tid, request_id=rid)
    assert retry["ok"] is True, retry
    assert retry["data"]["state"] == "in_progress"
    assert len(starts) == 1  # the committed start was recognised, not re-issued
    shown = ok(call(h, reader, action="show", subtask_id=tid))
    assert shown["subtask"]["state"] == "in_progress"
    assert shown["ownership_health"] == "held"


def test_external_reap_orchestrator_retires_token_then_recovers(covey, covey_bin, tmp_path, agent_for):
    # Real external reaper: `session exit` on the orchestrator mid-lifecycle.
    # The next orchestrator-registered action reports the failure (never
    # replayed) and a fresh explicit action registers a new orchestrator.
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id = ok(call(h, agent, action="create", prompt="seeded"))["meta_task_id"]
    (orch_principal,) = principals(db, "orchestrator")
    orch_token = db_tokens(db)[-1]  # the only registration is ours
    fresh_covey(covey_bin, str(db), "session", "exit", "--session-token", orch_token)
    # The SAME agent (and runtime) still holds the now-dead token.
    failed = call(h, agent, action="add", meta_task_id=meta_id, title="after reap")
    err(failed)  # the failed action is reported, never swallowed
    assert failed["data"]["retired_role"] == "orchestrator", failed
    assert_no_tokens(json.dumps(failed), actual_tokens=db_tokens(db))
    titles = [t["title"] for t in ok(call(h, reader, action="show",
                                          meta_task_id=meta_id))["subtasks"]]
    assert titles == []  # the reaped attempt never committed and was not replayed
    recovered = call(h, agent, action="add", meta_task_id=meta_id,
                     title="fresh orchestrator")
    assert recovered["ok"] is True, recovered  # fresh registration, not replay
    shown = ok(call(h, reader, action="show", meta_task_id=meta_id))
    assert [t["title"] for t in shown["subtasks"]] == ["fresh orchestrator"]
    orchestrators = principals(db, "orchestrator")
    assert len(orchestrators) == 2 and orchestrators[0] == orch_principal
    assert orchestrators[1] != orch_principal  # a fresh principal, never the dead one


def test_external_reap_idle_executor_next_start_recovers(covey, covey_bin, tmp_path, agent_for):
    # An executor that is registered and ACTIVE but holds no claim (it lost the
    # race for a task) is reaped externally. The next explicit start reports the
    # dead session without replaying, retires the token, and the following
    # explicit start claims under a fresh principal.
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    holder, loser = agent_for("holder"), agent_for("loser")
    meta_id, tid, orch = seed_worklist(covey_bin, db)
    extra = seed_task(covey_bin, db, orch, meta_id, "second task")
    ok(call(h, holder, action="start", subtask_id=tid))
    lost = call(h, loser, action="start", subtask_id=tid)  # loses the race for tid
    err(lost)
    assert_no_tokens(json.dumps(lost), actual_tokens=db_tokens(db))
    executors = principals(db, "executor")
    assert len(executors) == 2  # holder's, then the loser's active-but-unclaimed one
    (loser_token,) = [row[0] for row in db_query(
        db, "SELECT session_token FROM sessions WHERE agent_principal_id = ?",
        (executors[1],))]
    assert db_query(db, "SELECT active_subtask_id FROM sessions WHERE session_token = ?",
                    (loser_token,)) == [(None,)]  # unclaimed
    assert ok(call(h, holder, action="show", subtask_id=tid))["ownership_health"] == "held"
    # External reaper exits the idle executor.
    fresh_covey(covey_bin, str(db), "session", "exit", "--session-token", loser_token)
    failed = call(h, loser, action="start", subtask_id=extra)
    err(failed)
    assert failed["data"]["retired_role"] == "executor", failed
    assert ok(call(h, holder, action="show", subtask_id=extra))[
        "subtask"]["state"] == "available"  # the failed start was not replayed
    again = call(h, loser, action="start", subtask_id=extra)
    assert again["ok"] is True, again  # idle-executor reap self-recovers
    assert again["data"]["state"] == "in_progress"
    executors_now = principals(db, "executor")
    assert len(executors_now) == 3 and executors_now[2] not in executors[:2]
    assert ok(call(h, holder, action="show", subtask_id=tid))["ownership_health"] == "held"


def test_retired_request_refused_after_replacement(covey, covey_bin, tmp_path, agent_for):
    # Same request_id after the role token was retired must be refused, not
    # silently replayed under a replacement token.
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    agent = agent_for("s1")
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    rid = str(uuid.uuid4())
    ok(call(h, agent, action="start", subtask_id=tid, request_id=rid))
    rid2 = str(uuid.uuid4())
    ok(call(h, agent, action="retry", subtask_id=tid, summary="flaked",
            failure_code="FLAKE", request_id=rid2))
    replay = call(h, agent, action="start", subtask_id=tid, request_id=rid)
    assert replay["ok"] is False, replay  # retired token: refuse the replay
    assert replay["code"] == "replay_refused", replay
    assert ok(call(h, agent_for("s2"), action="show", subtask_id=tid))[
        "subtask"]["state"] == "available"  # the refusal had no effect


def test_retired_request_refused_after_normal_outcome(covey, covey_bin, tmp_path, agent_for):
    # Same refusal after a *successful* normal outcome (finish retires the
    # executor via exit): replaying a request with its EXACT original action
    # arguments is refused (changed arguments would instead be an
    # idempotency_conflict), even though the normal path itself succeeded.
    db = tmp_path / "d.db"
    h = covey(db_path=str(db))
    agent, reader = agent_for("s1"), agent_for("s2")
    meta_id, tid, _ = seed_worklist(covey_bin, db)
    rid = str(uuid.uuid4())
    ok(call(h, agent, action="start", subtask_id=tid, request_id=rid))
    rid2 = str(uuid.uuid4())
    fin = call(h, agent, action="finish", subtask_id=tid, summary="done",
               request_id=rid2)
    assert fin["ok"] is True, fin
    # Confirmed contract: `replay_refused` is permanent even after the
    # pending-exit slot clears and the normal outcome succeeded.
    replay_finish = call(h, agent, action="finish", subtask_id=tid, summary="done",
                         request_id=rid2)
    assert replay_finish["ok"] is False, replay_finish
    assert replay_finish["code"] == "replay_refused", replay_finish
    replay_start = call(h, agent, action="start", subtask_id=tid, request_id=rid)
    assert replay_start["ok"] is False and replay_start["code"] == "replay_refused", replay_start
    # Neither refused replay created a second receipt or changed the state.
    shown = ok(call(h, reader, action="show", subtask_id=tid))
    assert shown["subtask"]["state"] == "completed"
    assert [o["summary"] for o in shown["attempt_outcomes"]] == ["done"]


def test_transient_heartbeat_fault_uncertain_then_recovers(covey, covey_bin, agent_for, make_runtime, monkeypatch):
    # internal_error (e.g. SQLITE_BUSY after the 5s busy timeout) is NOT
    # definite authority loss: the claim is kept, marked uncertain, and the
    # SAME held authority recovers on the next tick / finish.
    h = covey(db_path="/unused/placeholder.db")
    mod = runtime_module(h)
    transient = {"on": True}

    def flaky_heartbeat(real, args, **kwargs):
        if is_cmd(args, "session", "heartbeat") and transient["on"]:
            return {"ok": False, "code": "internal_error",
                    "message": "database busy", "exit_code": 5}
        return real(args, **kwargs)

    rt = make_runtime(h, agent_for("s1"), lease_duration_ms=2000,
                      renew_interval_ms=100, heartbeat_interval_ms=100)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    with intercepted(monkeypatch, mod, flaky_heartbeat):
        started = ok(rt.execute({"action": "start", "subtask_id": tid}))
        stop_background(rt)
        tick_when_due(rt)  # the heartbeat is due and faults transiently
        status = ok(rt.execute({"action": "status"}))
        assert status["active_claim"] is not None  # authority retained
        assert status["active_claim"]["uncertain"] is True
        assert status["lost_reason"] is None
        transient["on"] = False
        tick_when_due(rt)  # uncertainty clears via authoritative reconciliation
        status = ok(rt.execute({"action": "status"}))
        assert status["active_claim"] is not None
        assert status["active_claim"]["uncertain"] is False
        assert started["claim_id"] == status["active_claim"]["claim_id"]
        fin = rt.execute({"action": "finish", "subtask_id": tid,
                          "summary": "survived transient fault"})
    assert fin["ok"] is True, fin  # claim was never stranded


def test_rapid_retire_restart_maintenance_is_heartbeating(covey, covey_bin, agent_for, make_runtime, monkeypatch):
    # Retire then immediately restart on a new claim: even if the retired
    # maintenance thread is still winding down, the NEW claim must get live
    # heartbeats from its OWN thread — identity-checked restart, never a
    # silently stopped loop.
    h = covey(db_path="/unused/placeholder.db")
    mod = runtime_module(h)
    hb_count = {"n": 0}

    def counting_heartbeat(real, args, **kwargs):
        if is_cmd(args, "session", "heartbeat"):
            hb_count["n"] += 1
        return real(args, **kwargs)

    rt = make_runtime(h, agent_for("s1"), lease_duration_ms=2000,
                      renew_interval_ms=100, heartbeat_interval_ms=100)
    meta_id, tid, orch = seed_worklist(covey_bin, rt.db_file)
    t1 = seed_task(covey_bin, rt.db_file, orch, meta_id, "second claim")
    with intercepted(monkeypatch, mod, counting_heartbeat):
        ok(rt.execute({"action": "start", "subtask_id": tid}))
        first_thread = rt._thread
        assert first_thread is not None
        assert wait_until(lambda: hb_count["n"] >= 1, timeout=15)  # claim 1 heartbeats
        rt._retire_executor("simulated rapid loss")
        ok(rt.execute({"action": "start", "subtask_id": t1}))  # immediate restart
        second_thread = rt._thread
        assert second_thread is not first_thread  # a fresh loop, not the stopped one
        assert second_thread.is_alive()
        after_restart = hb_count["n"]
        # The NEW claim is heartbeated by the restarted background loop itself.
        assert wait_until(lambda: hb_count["n"] > after_restart, timeout=15)
        fin = rt.execute({"action": "finish", "subtask_id": t1, "summary": "restarted"})
    assert fin["ok"] is True, fin
    first_thread.join(15)
    assert not first_thread.is_alive()  # the retired loop really wound down


def test_exception_text_redacted_in_warning_logs(covey, covey_bin, agent_for, make_runtime, monkeypatch, caplog):
    # R3: exceptions are sanitized before logging too, not just envelopes.
    h = covey(db_path="/unused/placeholder.db")
    mod = runtime_module(h)
    rt = make_runtime(h, agent_for("s1"))
    seed_worklist(covey_bin, rt.db_file)  # initialised DB: the read reaches the transport
    secret = f"session_{'0123abcd' * 4}"

    def tainted(real, args, **kwargs):
        raise ValueError(f"backend echoed session token {secret}")

    with intercepted(monkeypatch, mod, tainted):
        with caplog.at_level(logging.WARNING):
            out = rt.execute({"action": "lists"})
    err(out)
    assert out["code"] == "internal_error", out
    assert secret not in json.dumps(out), out
    assert secret not in caplog.text, caplog.text
    assert "session_[redacted]" in caplog.text, caplog.text


# ---------------------------------------------------------------------------
# AC11: structured resumable handoff
# ---------------------------------------------------------------------------

HANDOFF = {
    "objective": "fix flaky parser",
    "changes": ["touched parser.py"],
    "verification_refs": ["pytest tests/test_parser.py"],
    "failed_approaches": ["regex patch"],
    "remaining": ["handle unicode"],
}


def linked_worktree(tmp_path):
    """A canonical REAL linked Git worktree (init+commit the main repo, then
    git worktree add) so validation, cwd routing and the shared-identity
    lock exercise actual Git metadata, not synthetic fixtures."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    run(git + ["init", "-b", "main"])
    (repo / "file.txt").write_text("base\n")
    run(git + ["-c", "commit.gpgsign=false", "add", "file.txt"])
    run(git + ["-c", "commit.gpgsign=false", "commit", "-m", "base"])
    wt = tmp_path / "wt"
    run(git + ["worktree", "add", "--detach", str(wt)])
    return repo, wt


def run(cmd):
    import subprocess
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_handoff_codec_roundtrip_redaction_and_limits(covey, tmp_path, agent_for):
    h = covey(db_path=str(tmp_path / "d.db"))
    mod = runtime_module(h)
    token = f"session_{'a1b2c3d4' * 4}"
    bad = dict(HANDOFF, remaining=[f"check {token} on prod"])
    summary = mod.encode_handoff(bad, str(tmp_path))
    assert mod.HANDOFF_MARKER in summary
    assert token not in summary and mod.REDACTED in summary
    assert len(summary.encode()) <= mod.HANDOFF_MAX_BYTES
    assert "\n" not in summary and "\t" not in summary
    record = mod.decode_handoff(summary)
    assert record is not None
    assert record["objective"] == bad["objective"]
    assert record["remaining"] == [f"check {mod.REDACTED} on prod"]
    # malformed / unmarked inputs are never treated as validated records
    assert mod.decode_handoff("plain text summary") is None
    assert mod.decode_handoff("{not json") is None
    assert mod.decode_handoff(json.dumps({"marker": "covey-handoff.v9"})) is None
    stripped = {k: v for k, v in record.items() if k != "workspace"}
    assert mod.decode_handoff(json.dumps(stripped)) is None
    # byte limit enforced on the encoded bytes (multibyte payload)
    huge = dict(HANDOFF, objective="é" * (mod.HANDOFF_MAX_BYTES))
    with pytest.raises(mod.ActionReject) as excinfo:
        mod.encode_handoff(huge, str(tmp_path))
    assert excinfo.value.code == "invalid_arguments"


def test_checkpoint_persists_marked_handoff_and_releases_claim(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "h.db"
    h = covey(db_path=str(db))
    wt = str(linked_worktree(tmp_path)[1])
    _, tid, _ = seed_worklist(covey_bin, db)
    agent = agent_for("w1")
    ok(call(h, agent, action="start", subtask_id=tid, workspace=wt))
    fin = ok(call(h, agent, action="checkpoint", subtask_id=tid,
                   handoff=dict(HANDOFF, remaining=["next: run tests"])))
    assert fin["state"] == "available"
    shown = ok(call(h, agent_for("r"), action="resume", subtask_id=tid))
    handoffs = shown["resume_handoffs"]
    assert len(handoffs) == 1
    assert handoffs[0]["validated_handoff"] is not None
    assert handoffs[0]["validated_handoff"]["remaining"] == ["next: run tests"]
    # provenance stays bound to the original claim/fence, token-free
    assert handoffs[0]["outcome_kind"] == "retryable_failure"
    assert isinstance(handoffs[0]["claim_id"], str)
    assert handoffs[0]["fence_seq"] >= 1
    assert shown["subtask"]["state"] == "available"
    # the release happened atomically with the receipt
    assert shown["ownership_health"] == "unclaimed"


def test_checkpoint_evidence_policy_preserved(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "ev.db"
    meta_id, _, orch = seed_worklist(covey_bin, db)
    tid = fresh_covey(covey_bin, db, "subtask", "create",
                      "--session-token", orch,
                      "--meta-task-id", meta_id, "--title", "cp needs evidence",
                      "--completion-policy", "direct",
                      "--evidence-policy", "required",
                      "--routing-key", "default")["subtask_id"]
    h = covey(db_path=str(db))
    agent = agent_for("a")
    ok(call(h, agent, action="start", subtask_id=tid))
    refused = err(call(h, agent, action="checkpoint", subtask_id=tid, handoff=HANDOFF))
    assert refused["code"] == "evidence_required"
    done = ok(call(h, agent, action="checkpoint", subtask_id=tid, handoff=HANDOFF,
                   evidence_digest="blake3:abc"))
    assert done["state"] == "available"


def test_resume_returns_unmarked_summaries_unverified(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "r.db"
    h = covey(db_path=str(db))
    _, tid, _ = seed_worklist(covey_bin, db)
    ok(call(h, agent_for("a"), action="start", subtask_id=tid))
    ok(call(h, agent_for("a"), action="retry", subtask_id=tid,
            summary="I fixed it (trust me)", failure_code="done"))
    shown = ok(call(h, agent_for("b"), action="resume", subtask_id=tid))
    handoff = shown["resume_handoffs"][0]
    assert handoff["validated_handoff"] is None
    assert handoff["unverified_summary"] == "I fixed it (trust me)"


def test_workspace_start_refuses_non_worktree_paths(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "ws.db"
    h = covey(db_path=str(db))
    repo, wt = linked_worktree(tmp_path)
    meta_id, tid, orch = seed_worklist(covey_bin, db)
    plain = tmp_path / "plain"; plain.mkdir()
    # Real non-Git path and REAL main checkout are both refused (the
    # main-checkout gitdir == common-dir, which the guard rejects); a real
    # linked worktree is accepted.
    refused = err(call(h, agent_for("a"), action="start", subtask_id=tid,
                       workspace=str(plain)))
    assert refused["code"] == "workspace_invalid"
    refused = err(call(h, agent_for("a"), action="start", subtask_id=tid,
                       workspace=str(repo)))
    assert refused["code"] == "workspace_invalid"
    ok(call(h, agent_for("a"), action="start", subtask_id=tid, workspace=str(wt)))
    # legacy unbound start still works (coordination-only), no global veto:
    # the second seeded task starts on a different agent without a workspace
    second = seed_task(covey_bin, db, orch, meta_id, "legacy")
    ok(call(h, agent_for("b"), action="start", subtask_id=second))


def test_workspace_guard_blocks_outside_mutation_then_closes(covey, covey_bin, tmp_path, agent_for,
                                                             make_runtime):
    # Real runtime + real claim: the guard runs on the ACTUAL
    # _workspace_guard code path used by the registered pre_tool_call hook,
    # with a real live claim installed via start(), then a REAL expiry of the
    # real lease (honest short lease, engine expiry) flips it closed.
    h = covey(db_path="/unused/placeholder.db")
    _, wt = linked_worktree(tmp_path)
    wt = str(wt)
    rt = make_runtime(h, agent_for("g"), lease_duration_ms=400,
                      renew_interval_ms=10000, heartbeat_interval_ms=10000)
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    started = ok(rt.execute({"action": "start", "subtask_id": tid, "workspace": wt}))
    assert started["workspace"] == wt
    # live claim: inside file + terminal allowed; outside file/patch refused
    inside = f"{wt}/f.py"
    assert rt.workspace_guard("write_file", {"path": inside}) is None
    assert rt.workspace_guard("terminal", {"command": "pytest"}) is None
    # relative targets resolve INSIDE the workspace (legitimate work)
    assert rt.workspace_guard("write_file", {"path": "f.py"}) is None
    # V4A headers are the actual patch targets patch_tool() mutates
    v4a = (f"*** Begin Patch\n*** Update File: {tmp_path}/evil.py\n"
           "+x\n*** End Patch\n")
    assert rt.workspace_guard("patch", {"mode": "patch", "patch": v4a})["code"] \
        == "workspace_mutation_outside"
    assert rt.workspace_guard("write_file", {"path": str(tmp_path / "evil.py")})["code"] \
        == "workspace_mutation_outside"
    # REAL lease expiry: claim is retired truthfully, guard flips closed
    stop_background(rt)
    sleep_until_ms(started["lease_deadline_ms"] + 500)
    rt.maintenance_once()  # maintenance notices the real expiry
    assert rt.workspace_guard("terminal", {"command": "ls"})["code"] == "workspace_guard_closed"
    assert rt.workspace_guard("write_file", {"path": inside})["code"] == "workspace_guard_closed"
    # native read-only process actions stay exempt for truthful handoff
    assert rt.workspace_guard("process_manage", {"action": "poll"}) is None


def test_workspace_guard_never_engages_unbound_runtime(covey, covey_bin, tmp_path, agent_for,
                                                       make_runtime):
    h = covey(db_path="/unused/placeholder.db")
    rt = make_runtime(h, agent_for("u"))
    meta_id, tid, _ = seed_worklist(covey_bin, rt.db_file)
    ok(rt.execute({"action": "start", "subtask_id": tid}))  # unbound claim
    assert rt._guarded_workspace is None
    assert rt.workspace_guard("terminal", {"command": "ls"}) is None


def test_worktree_flock_serializes_start_across_runtimes(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "lock.db"
    h = covey(db_path=str(db))
    repo, wt = linked_worktree(tmp_path)
    _, tid, _ = seed_worklist(covey_bin, db)
    ok(call(h, agent_for("owner"), action="start", subtask_id=tid, workspace=str(wt)))
    # machine-wide lock lives in the SHARED git identity (real gitdir), not per-profile
    assert any(p.name == "covey-tasks.lock" for p in (repo / ".git").rglob("covey-tasks.lock"))
    # the flock itself blocks peers on a DIFFERENT task while held
    mod = runtime_module(h)
    peer_rt = mod.CoveyRuntime(agent_for("peer"), binary=covey_bin, db_path=str(db),
                               profile_home=str(tmp_path))
    try:
        busy = peer_rt._acquire_workspace_lock(str(wt))
        assert busy is not None and busy["code"] == "workspace_busy"
    finally:
        peer_rt.close()


def test_read_diagnostics_filters_and_bounds(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "diag.db"
    h = covey(db_path=str(db))
    for bad in ({"action": "candidates", "role": "manager"},
                {"action": "candidates", "role": "executor", "limit": 9999},
                {"action": "stuck", "older_than_ms": 0},
                {"action": "stuck", "older_than_ms": 1, "limit": 500}):
        assert err(call(h, agent_for("d"), **bad))["code"] == "invalid_arguments"
    _, tid, _ = seed_worklist(covey_bin, db)
    base = covey_events(covey_bin, db)
    tok_before = db_tokens(db)
    got = ok(call(h, agent_for("d"), action="candidates", role="executor"))
    assert tid in [item["subtask_id"] for item in got["items"]]  # native flat rows projected
    ok(call(h, agent_for("d"), action="availability"))
    ok(call(h, agent_for("d"), action="stuck", older_than_ms=3600000))
    assert db_tokens(db) == tok_before  # reads never register sessions
    assert covey_events(covey_bin, db) == base  # reads never write events
    # empty DB path still short-circuits without creating anything
    empty = tmp_path / "empty.db"
    h2 = covey(db_path=str(empty))
    err4 = err(call(h2, agent_for("e"), action="candidates", role="executor"))
    assert err4["code"] == "not_initialized"
    assert not empty.exists()




def test_bound_claim_guards_actual_native_file_dispatch(covey, covey_bin, tmp_path, agent_for, monkeypatch):
    from agent.subagent_lifecycle import bind_subagent_parent
    monkeypatch.chdir(tmp_path)

    db = tmp_path / "dispatch.db"
    h = covey(db_path=str(db))
    _, workspace = linked_worktree(tmp_path)
    _, task, _ = seed_worklist(covey_bin, db)
    owner, peer = agent_for("dispatch-owner"), agent_for("dispatch-peer")
    owner._current_task_id = "file-task-not-session"
    ok(call(h, owner, action="start", subtask_id=task, workspace=str(workspace)))
    owner._current_task_id = "rotated-file-task"
    outside = tmp_path / "outside.py"

    def dispatch(agent, name, arguments):
        from model_tools import handle_function_call
        with bind_subagent_parent(agent):
            return handle_function_call(
                name, arguments, session_id=agent.session_id,
                task_id=getattr(agent, "_current_task_id", agent.session_id),
                tool_call_id=uuid.uuid4().hex)

    dispatch(owner, "write_file", {"path": "owned.py", "content": "inside\n"})
    assert (workspace / "owned.py").read_text() == "inside\n"
    dispatch(owner, "write_file", {"path": str(outside), "content": "escaped\n"})
    assert not outside.exists()
    patch = f"*** Begin Patch\n*** Add File: {outside}\n+escaped\n*** End Patch"
    dispatch(owner, "patch", {"mode": "patch", "patch": patch})
    assert not outside.exists()
    ok(call(h, owner, action="finish", subtask_id=task, summary="guard exercise"))
    dispatch(owner, "write_file", {"path": str(workspace / "owned.py"), "content": "stale owner\n"})
    assert (workspace / "owned.py").read_text() == "inside\n"
    dispatch(peer, "write_file", {"path": str(outside), "content": "independent peer\n"})
    assert outside.read_text() == "independent peer\n"


def test_bound_release_then_unbound_start_clears_stale_guard(covey, covey_bin, tmp_path,
                                                             agent_for, make_runtime,
                                                             monkeypatch):
    # bound claim -> release (guard stays closed) -> SUCCESSFUL legacy unbound
    # start (guard dropped, native writes legal again).  Same runtime, real
    # covey, real native dispatch — no new binding is created for the old
    # workspace.
    from agent.subagent_lifecycle import bind_subagent_parent
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "lifecycle.db"
    h = covey(db_path=str(db))
    _, workspace = linked_worktree(tmp_path)
    meta_id, task, seed_orch = seed_worklist(covey_bin, db)
    rt = make_runtime(h, agent_for("lc"), db=db.name)
    started = ok(rt.execute({"action": "start", "subtask_id": task,
                             "workspace": str(workspace)}))
    ok(rt.execute({"action": "release", "subtask_id": task}))
    # the stale guard remains until a fresh start actually succeeds
    assert rt._guarded_workspace == str(workspace)

    second = seed_task(covey_bin, db, seed_orch, meta_id, "legacy follow-up")
    ok(rt.execute({"action": "start", "subtask_id": second}))  # unbound legacy claim
    assert rt._guarded_workspace is None  # previous binding NOT re-inherited

    agent = agent_for("lc")
    from model_tools import handle_function_call
    outside = tmp_path / "legacy.py"
    with bind_subagent_parent(agent):
        handle_function_call(
            "write_file", {"path": str(outside), "content": "legacy\n"},
            session_id=agent.session_id,
            task_id=getattr(agent, "_current_task_id", agent.session_id),
            tool_call_id=uuid.uuid4().hex)
    assert outside.read_text() == "legacy\n"


def test_absent_cwd_records_cleared_on_release(covey, covey_bin, tmp_path, agent_for,
                                               make_runtime):
    # record_session_cwd ignores None/empty values, so absent records must be
    # REMOVED via clear_session_cwd on release; restoring with None is a no-op
    # and would leave the agent pinned to the released worktree.
    from tools.terminal_tool import get_session_cwd, record_session_cwd
    db = tmp_path / "cwd.db"
    h = covey(db_path=str(db))
    _, workspace = linked_worktree(tmp_path)
    meta_id, task, _ = seed_worklist(covey_bin, db)
    agent = agent_for("cwd-agent")
    agent._current_task_id = "cwd-task"
    record_session_cwd(agent.session_id, None)  # absent prior (record not created)
    record_session_cwd(agent._current_task_id, None)
    rt = make_runtime(h, agent, db=db.name)
    ok(rt.execute({"action": "start", "subtask_id": task, "workspace": str(workspace)}))
    assert get_session_cwd(agent.session_id) == str(workspace)
    assert get_session_cwd(agent._current_task_id) == str(workspace)
    ok(rt.execute({"action": "release", "subtask_id": task}))
    assert get_session_cwd(agent.session_id) is None
    assert get_session_cwd(agent._current_task_id) is None


def test_prior_cwd_record_restored_on_release(covey, covey_bin, tmp_path, agent_for,
                                              make_runtime):
    from tools.terminal_tool import get_session_cwd, record_session_cwd
    db = tmp_path / "cwd2.db"
    h = covey(db_path=str(db))
    _, workspace = linked_worktree(tmp_path)
    meta_id, task, _ = seed_worklist(covey_bin, db)
    agent = agent_for("prior-agent")
    prior = str(tmp_path / "prior-cwd")
    (tmp_path / "prior-cwd").mkdir()
    record_session_cwd(agent.session_id, prior)
    rt = make_runtime(h, agent, db=db.name)
    ok(rt.execute({"action": "start", "subtask_id": task, "workspace": str(workspace)}))
    assert get_session_cwd(agent.session_id) == str(workspace)
    ok(rt.execute({"action": "release", "subtask_id": task}))
    assert get_session_cwd(agent.session_id) == prior


def test_guard_selects_current_database_and_never_leaks_across_dbs(
        covey, covey_bin, tmp_path, agent_for, monkeypatch):
    # Same profile, same live agent object: when the profile's current db_path
    # changes, the pre-tool hook must follow the (agent, profile, canonical-db)
    # key covey_tasks uses — never the runtime of a different database.
    import yaml
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools.terminal_tool import get_session_cwd
    monkeypatch.chdir(tmp_path)

    def blocked_by_guard(plugin, tool, args):
        result = plugin.hook("pre_tool_call", tool_name=tool, args=args)
        return result[0] if result else None

    def use_db(plugin, path):
        cfg = yaml.safe_load((plugin.home / "config.yaml").read_text())
        cfg["plugins"]["entries"]["covey-tasks"]["settings"]["db_path"] = str(path)
        (plugin.home / "config.yaml").write_text(yaml.safe_dump(cfg))

    parent1 = tmp_path / "one"; parent1.mkdir()
    parent2 = tmp_path / "two"; parent2.mkdir()
    _, wt1 = linked_worktree(parent1)
    _, wt2 = linked_worktree(parent2)
    db1 = tmp_path / "a.db"
    db2 = tmp_path / "b.db"
    agent = agent_for("dual-db")

    h = covey(db_path=str(db1))
    _, t1, _ = seed_worklist(covey_bin, db1)
    ok(call(h, agent, action="start", subtask_id=t1, workspace=str(wt1)))
    outside = tmp_path / "other-db.py"

    # Guard engages for the CURRENT database's bound claim.
    with bind_subagent_parent(agent):
        assert blocked_by_guard(h, "write_file",
                                {"path": str(outside), "content": "x\n"})

    # Switch the profile to the second database: the first database's guard
    # must not block, and no stale cwd routing leaks into the new runtime.
    use_db(h, db2)
    _, t2, _ = seed_worklist(covey_bin, db2)
    with bind_subagent_parent(agent):
        assert blocked_by_guard(h, "write_file",
                                {"path": str(outside), "content": "x\n"}) is None
        # The old database's cwd routing must not be inherited by the new
        # runtime before any claim exists there: switching DBs releases the
        # released claim's cwd, and t2's start rebinds it to wt2 below.
        assert get_session_cwd(agent.session_id) != str(wt1), \
            "switching databases must not leak the retired workspace as cwd"
        ok(call(h, agent, action="start", subtask_id=t2, workspace=str(wt2)))
    assert get_session_cwd(agent.session_id) == str(wt2)
    with bind_subagent_parent(agent):
        assert blocked_by_guard(h, "write_file",
                                {"path": str(outside), "content": "x\n"})

    # Switch back: t1's claim is untouched and still releasable through the
    # first database's runtime; the second database's live guard keeps working.
    use_db(h, db1)
    # Switching back must restore the live workspace association, not just
    # leave the task claimable: the guard hook rebinds native cwd to wt1,
    # while runtime2's claim/flock for db2 stays untouched.
    with bind_subagent_parent(agent):
        assert blocked_by_guard(h, "write_file",
                                {"path": str(outside), "content": "x\n"})
        assert get_session_cwd(agent.session_id) == str(wt1), \
            "switching back to the claiming database must restore its workspace cwd"
    released = ok(call(h, agent, action="release", subtask_id=t1))
    assert released["state"] == "available"  # release returns the task to the pool
    use_db(h, db2)

    with bind_subagent_parent(agent):
        assert blocked_by_guard(h, "write_file",
                                {"path": str(outside), "content": "x\n"}), \
            "switching databases must not disarm the live claim's guard"

def test_uncertain_unbound_claim_never_rebinds_released_workspace(
        covey, covey_bin, tmp_path, agent_for, monkeypatch):
    # Bound start -> release (guard stays closed) -> unbound replacement whose
    # `subtask start` times out (real CLI fault injected at the transport
    # seam). The hook must never rebind the released workspace for an unbound
    # claim; the authoritative cwd is restored. After the same request_id
    # replays into a real successful start, native relative writes land
    # outside the old worktree.
    from agent.subagent_lifecycle import bind_subagent_parent
    from model_tools import handle_function_call
    from tools.terminal_tool import get_session_cwd, record_session_cwd
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "uncertain.db"
    h = covey(db_path=str(db))
    _, workspace = linked_worktree(tmp_path)
    meta_id, task, orch = seed_worklist(covey_bin, db)
    agent = agent_for("uncertain")
    prior = str(tmp_path / "prior-cwd")
    (tmp_path / "prior-cwd").mkdir()
    record_session_cwd(agent.session_id, prior)

    ok(call(h, agent, action="start", subtask_id=task, workspace=str(workspace)))
    ok(call(h, agent, action="release", subtask_id=task))
    second = seed_task(covey_bin, db, orch, meta_id, "unbound follow-up")

    mod = runtime_module(h)
    start_seen = False

    def partial_start(real, args, **kwargs):
        nonlocal start_seen
        if tuple(args[:2]) == ("subtask", "start") and not start_seen:
            start_seen = True
            return TIMEOUT_ENVELOPE
        return real(args, **kwargs)

    rid = str(uuid.uuid4())
    with intercepted(monkeypatch, mod, partial_start):
        partial = err(call(h, agent, action="start", subtask_id=second, request_id=rid))
    assert partial["code"] == "outcome_unknown", partial

    with bind_subagent_parent(agent):
        blocked = h.hook("pre_tool_call", tool_name="terminal", args={"command": "pytest"})
    assert blocked and blocked[0]["message"], "retained guard must stay closed"
    assert get_session_cwd(agent.session_id) == prior, \
        "unbound replacement claim must not rebind the released workspace"

    ok(call(h, agent, action="start", subtask_id=second, request_id=rid))

    with bind_subagent_parent(agent):
        passed = h.hook("pre_tool_call", tool_name="terminal", args={"command": "echo ok"})
    assert not passed, "successful unbound start clears the stale guard"
    assert get_session_cwd(agent.session_id) == prior

    # The guard is open for the unbound claim, so native relative resolution
    # is authoritative: a relative path resolves against the restored prior
    # directory and must never execute inside the released old worktree.
    rel = "follow-up.py"
    with bind_subagent_parent(agent):
        handle_function_call(
            "write_file", {"path": rel, "content": "unbound\n"},
            session_id=agent.session_id,
            task_id=getattr(agent, "_current_task_id", agent.session_id),
            tool_call_id=uuid.uuid4().hex)
    assert (tmp_path / "prior-cwd" / rel).read_text() == "unbound\n"
    assert not (workspace / rel).exists()

def test_relative_gitdir_lock_resolves_from_worktree(covey, covey_bin, tmp_path, agent_for,
                                                     monkeypatch):
    # A linked worktree created with RELATIVE git metadata (gitdir: ../.git/...)
    # must resolve its lock through the worktree's actual absolute gitdir, not
    # against the Hermes process cwd.
    db = tmp_path / "relgit.db"
    h = covey(db_path=str(db))
    repo, workspace = linked_worktree(tmp_path)
    (repo / ".git" / "worktrees" / "wt" / "gitdir").write_text("../worktrees/wt/\n")
    meta_id, task, _ = seed_worklist(covey_bin, db)
    monkeypatch.chdir(tmp_path)  # process cwd is NOT the gitdir's parent
    ok(call(h, agent_for("rel-owner"), action="start", subtask_id=task,
            workspace=str(workspace)))
    # A competing owner cannot acquire the same worktree identity
    mod = runtime_module(h)
    peer_rt = mod.CoveyRuntime(agent_for("rel-peer"), binary=covey_bin,
                               db_path=str(db), profile_home=str(tmp_path))
    try:
        busy = peer_rt._acquire_workspace_lock(str(workspace))
        assert busy is not None and busy["code"] == "workspace_busy"
    finally:
        peer_rt.close()


def test_static_guidance_shown_once_per_runtime(covey, tmp_path, agent_for):
    db = tmp_path / "guid.db"
    h = covey(db_path=str(db))
    agent = agent_for("s1")
    first = ok(call(h, agent, action="create", prompt="guidance one"))
    assert first["guidance"]  # static guidance on the FIRST qualifying result
    second = ok(call(h, agent, action="create", prompt="guidance two"))
    assert "guidance" not in second  # once per runtime, not per result


def test_start_guidance_shown_once_per_runtime(covey, covey_bin, tmp_path, agent_for):
    db = tmp_path / "guid-start.db"
    h = covey(db_path=str(db))
    mod = runtime_module(h)
    meta_id, tid, orch = seed_worklist(covey_bin, db)
    tid2 = seed_task(covey_bin, db, orch, meta_id, "second claim")
    first_rt = mod.CoveyRuntime(agent_for("s1"), binary=covey_bin,
                                db_path=str(db), profile_home=str(tmp_path))
    second_rt = mod.CoveyRuntime(agent_for("s2"), binary=covey_bin,
                                 db_path=str(db), profile_home=str(tmp_path))
    try:
        started = ok(first_rt.execute({"action": "start", "subtask_id": tid}))
        assert started["guidance"]
        fresh = ok(second_rt.execute({"action": "start", "subtask_id": tid2}))
        # A fresh runtime shows the guidance again; the previous runtime
        # does not repeat it.
        assert fresh["guidance"]
        assert first_rt._started_data(first_rt._claim)["state"] == "in_progress"
        assert "guidance" not in first_rt._started_data(first_rt._claim)
    finally:
        first_rt.close()
        second_rt.close()
