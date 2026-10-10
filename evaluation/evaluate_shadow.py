#!/usr/bin/env python3
"""Six-case observational shadow evaluation. Offline unless --run is explicit.

Offline: python3 evaluation/evaluate_shadow.py --output /tmp/shadow-baseline
Live: use the Hermes environment's python3 and add --run. Each native child has
an isolated home/process, explicit xhigh effort, and a 240-second outer limit.
This is not an OS sandbox: terminal tools execute trusted local coding tasks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request

MODEL = "qwen3.8-flash-next"
BASE_URL = "http://localhost:8001/v1"
CLASSIFIER = "http://192.168.50.149:8787"
SEED = 20261009
LIMIT = 16384
SECRETS = []
ALLOWED_EFFORTS = ("low", "medium", "high", "xhigh")
_OBS_LOCK = threading.Lock()
WORKFLOW = (" Work only in the current directory. Validate by running "
            "`python3 -m pytest -q` there. Never modify or delete test_public.py, "
            "and do not create or read any hidden evaluator files.")


def redact(text):
    for secret in SECRETS:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = redact(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def digest(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return None
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_hashes(workspace):
    return {str(p.relative_to(workspace)): digest(p)
            for p in sorted(workspace.rglob("*.py"))
            if not any(part.startswith(".") or part == "__pycache__"
                       for part in p.relative_to(workspace).parts)
            and not p.name.startswith("test_") and p.name != "conftest.py"}


def clean_env():
    # Carry only runtime/module paths, never credential or profile vars.
    allowed = ("PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH",
               "SSL_CERT_FILE", "SSL_CERT_DIR")
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    result["PATH"] = str(Path(sys.executable).parent) + os.pathsep + result.get("PATH", "")
    result.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    return result


def bounded(command, cwd, timeout, *, env=None, stdin=None):
    start = time.monotonic()
    buffers = [bytearray(), bytearray()]
    totals = [0, 0]
    process = subprocess.Popen(command, cwd=cwd, env=env or clean_env(),
                               stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

    def drain(stream, index):
        while chunk := stream.read(4096):
            totals[index] += len(chunk)
            buffers[index].extend(chunk[:max(0, LIMIT - len(buffers[index]))])
        stream.close()

    threads = [threading.Thread(target=drain, args=(stream, index), daemon=True)
               for index, stream in enumerate((process.stdout, process.stderr))]
    for thread in threads:
        thread.start()
    if stdin is not None:
        try:
            process.stdin.write(stdin.encode())
            process.stdin.close()
        except BrokenPipeError:
            pass
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Also reap background terminal processes left behind by a completed worker.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for thread in threads:
            thread.join(timeout=2)
    return {"returncode": process.returncode, "timed_out": timed_out,
            "duration_seconds": round(time.monotonic() - start, 3),
            "stdout": redact(buffers[0].decode("utf-8", "replace")),
            "stderr": redact(buffers[1].decode("utf-8", "replace")),
            "stdout_bytes": totals[0], "stderr_bytes": totals[1],
            "output_truncated": any(size > LIMIT for size in totals)}


def materialize(case, workspace):
    workspace.mkdir(parents=True)
    for name, content in case["initial_files"].items():
        path = workspace / name
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Case contains unsafe source path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    public = "def test_public():\n" + textwrap.indent(case["public_check"], "    ")
    (workspace / "test_public.py").write_text(public, encoding="utf-8")
    (workspace / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["test_public.py"]\n', encoding="utf-8")
    return digest(workspace / "test_public.py")


def check(script, workspace):
    compile(script, "<check>", "exec")  # reject broken check scripts, not cases
    result = bounded([sys.executable, "-B", "-c", script], workspace, 20)
    result["passed"] = result["returncode"] == 0 and not result["timed_out"]
    # Public baseline must be a genuine AssertionError; heldout initial failure
    # may legitimately be another exception (e.g. ValueError from parsing).
    result["assertion_failure"] = (result["returncode"] == 1 and not result["timed_out"]
                                   and "AssertionError" in result["stderr"])
    result["expected_failure"] = result["returncode"] != 0 and not result["timed_out"]
    return result


def aggregate(results, *, mode):
    attempted = len(results)
    infra = [r for r in results if r.get("infra_error")]
    evaluable = [r for r in results if r.get("evaluable")]
    groups = {}
    for result in evaluable:
        verdict = (result.get("classifier") or {}).get("verdict")
        label = verdict if verdict is not None else "unclassified"
        row = groups.setdefault(label, {"n": 0, "correct": 0, "corrective_cycles": 0,
                                       "corrective_observations": 0,
                                       "latency_seconds": [], "tokens": [], "cost_available": 0})
        row["n"] += 1
        row["correct"] += int(result.get("correct") is True)
        telemetry = result.get("telemetry", {})
        # Zero rework counts are meaningful only when verification was
        # observable; otherwise corrective cycles are UNKNOWN (0 obs).
        if telemetry.get("verification_ledger_rows_observed", 0) > 0:
            row["corrective_observations"] += 1
            row["corrective_cycles"] += telemetry.get("verified_corrective_revisions", 0)
        native = result.get("native", {})
        if isinstance(native.get("duration_seconds"), (int, float)):
            row["latency_seconds"].append(native["duration_seconds"])
        tokens = native.get("tokens") or {}
        if all(isinstance(tokens.get(k), (int, float)) for k in ("input", "output")):
            row["tokens"].append(tokens["input"] + tokens["output"])
        row["cost_available"] += int(native.get("cost_usd") is not None)
    for row in groups.values():
        row["correctness_rate"] = row["correct"] / row["n"]
        row["corrective_cycles_observed"] = (row["corrective_observations"] > 0)
        for name in ("latency_seconds", "tokens"):
            values = row.pop(name)
            row[name + "_observations"] = len(values)
            row[name + "_mean"] = sum(values) / len(values) if values else None
    scored = sum(r.get("correct") in (True, False) for r in evaluable)
    return {"attempted": attempted, "evaluable": len(evaluable), "infra_errors": len(infra),
            "expected_scored_observations": 6, "scored_observations": scored,
            "evaluation_complete": scored >= 6 if mode == "live" else False,
            "corrective_cycles": "UNKNOWN (0 verification-observable observations)"
                                 if all(r.get("telemetry", {}).get("verification_ledger_rows_observed", 0) == 0
                                        for r in evaluable) else "observed",
            "by_classifier_label": groups, "automatic_routing": "NOT_QUALIFIED",
            "design": "N=6 purpose-made cases; observational explicit-xhigh only; no lower-effort causal claim",
            "cost_qualification": "failed: unknown/estimated local cost is not actual billing"}


def run_native(args, request):
    """One case in a native Hermes worker; runs ONLY inside --run --worker.

    Imports happen after HERMES_HOME is pinned, so no state touches the real
    profile. The parent AIAgent is a donor (model/credential/toolset source);
    its conversation is never run. Child effort is the parent's reasoning dict
    verbatim (effort inheritance); the subagent-effort plugin observes only.
    """
    effort = request.get("effort", "xhigh")
    if effort not in ALLOWED_EFFORTS:
        raise ValueError("unsupported requested effort")
    seed = int(request.get("seed", SEED))  # caller-supplied paired seed; default unchanged
    workspace = Path(request["workspace"])
    home = Path(request["hermes_home"])
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    os.environ.setdefault("HERMES_PROFILE", "default")
    # Credential handling (security-reviewed design): read the dotenv value
    # once, publish it under the AUXILIARY_*_API_KEY name the native scrubber
    # strips (agent/local_env_policy.py:173-181), and remove
    # CLASSIFIER_API_KEY from the worker environ BEFORE parent/terminal init,
    # so no session export -p snapshot or child terminal command can carry it.
    # The plugin reads the same-named var through its config override.
    original_key = os.environ.pop("CLASSIFIER_API_KEY", None)
    try:
        from dotenv import dotenv_values
        if original_key is None and args.env_file.is_file():
            original_key = dotenv_values(str(args.env_file)).get("CLASSIFIER_API_KEY")
    except ImportError:
        pass
    if not original_key:
        raise SystemExit("CLASSIFIER_API_KEY missing; refusing expensive run without the classifier")
    os.environ["AUXILIARY_SHADOW_CLASSIFIER_API_KEY"] = original_key
    SECRETS.extend(v for v in (original_key, os.environ.get("LOCAL_MODEL_API_KEY", "")) if v)
    # Native auto-discovery: the real manager scans <home>/plugins for
    # manifests and honours plugins.enabled. No get_plugin_manager monkeypatch.
    (home / "plugins").mkdir(exist_ok=True)
    (home / "plugins" / "subagent-effort").symlink_to(
        Path(__file__).resolve().parent.parent / "plugins" / "subagent-effort")
    import yaml
    config = {
        "terminal": {"backend": "local"},
        "agent": {"verify_on_stop": True, "max_verify_nudges": 2},
        "delegation": {"max_iterations": 12, "child_timeout_seconds": 180,
                       "subagent_auto_approve": False},
        "auxiliary": {"background_review": {"enabled": False}},
        "plugins": {"enabled": ["subagent-effort"],
                    "entries": {"subagent-effort": {"enabled": True,
                                                    "settings": {"endpoint": CLASSIFIER,
                                                                 "api_key_env":
                                                                     "AUXILIARY_SHADOW_CLASSIFIER_API_KEY"}}}},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))

    from hermes_cli.plugins import (discover_plugins, get_plugin_manager,
                                    get_plugin_command_handler, PluginContext)
    from tools.terminal_tool import record_session_cwd, clear_session_cwd
    from agent.runtime_cwd import set_session_cwd, clear_session_cwd as clear_runtime_cwd

    # get_plugin_manager() alone only constructs the per-home CACHED manager
    # (plugins.py:1526); the public discover_plugins() triggers
    # manager.discover_and_load against the isolated home. No monkeypatch.
    discover_plugins()
    manager = get_plugin_manager()

    loaded = manager._plugins.get("subagent-effort")
    assert loaded and loaded.enabled, f"native loader refused subagent-effort: {getattr(loaded, 'error', None)}"

    # Native observer on the same plugin context: source snapshots per tool
    # call; failed native verification evidence followed by a later source
    # change is counted once as a verified corrective revision. _OBS_LOCK
    # serializes the critical section because tool hooks fire concurrently.
    telemetry = {"tool_events": [], "source_changes": [],
                 "verified_corrective_revisions": 0,
                 "corrective_revision_events": [], "child_session_id": None,
                 "_prev_hashes": source_hashes(workspace)}

    def on_tool(*, tool_name="", args=None, result=None, status=None,
                tool_call_id="", session_id="", error_type=None,
                error_message=None, duration_ms=None, **_kw):
        with _OBS_LOCK:
            post = source_hashes(workspace)
            prev = telemetry["_prev_hashes"]
            if post != prev:
                telemetry["source_changes"].append({
                    "tool_call_id": tool_call_id, "tool": tool_name,
                    "changed": sorted(name for name in set(prev) | set(post)
                                      if prev.get(name) != post.get(name))})
                # Failed verification followed by THIS change = one verified
                # corrective revision; consumed so each failure counts once.
                if telemetry.pop("_failed_pending", None) is not None:
                    telemetry["verified_corrective_revisions"] += 1
                    telemetry["corrective_revision_events"].append(
                        {"failed_verification_marker": "tool_result.verification_evidence",
                         "change_tool_call_id": tool_call_id})
            telemetry["_prev_hashes"] = post
            # native terminal_tool_result.py:254-273 attaches
            # verification_evidence then json.dumps()s the result;
            # model_tools.py:681-684 forwards the string unchanged. Decode it.
            evidence = None
            if isinstance(result, str):
                try:
                    evidence = json.loads(result).get("verification_evidence")
                except (ValueError, AttributeError):
                    evidence = "unknown"
            elif isinstance(result, dict):
                evidence = result.get("verification_evidence")
            if isinstance(evidence, dict) and evidence.get("status") == "failed":
                telemetry["_failed_pending"] = {
                    "tool_call_id": tool_call_id,
                    "evidence": {k: evidence.get(k)
                                 for k in ("status", "exit_code", "canonical_command")}}
            # Absence of evidence is recorded as None, not zero-observable
            # rework; undecodable results are recorded "unknown".
            evidence_status = ("unknown" if evidence == "unknown"
                               else evidence.get("status") if isinstance(evidence, dict) else None)
            telemetry["tool_events"].append({
                "tool_call_id": tool_call_id, "tool": tool_name, "status": status,
                "error_type": error_type,
                "error_message": redact(str(error_message))[:200] if error_message else None,
                "duration_ms": duration_ms, "session_id": session_id,
                "verification_evidence_status": evidence_status})
        return None  # observer only; feedback never reaches the agent

    plugin_ctx = PluginContext(loaded.manifest, manager)  # real context, same as fixture
    registration = plugin_ctx.register_hook("post_tool_call", on_tool)

    def on_start(**kw):
        with _OBS_LOCK:
            if kw.get("child_session_id"):
                telemetry["child_session_id"] = kw["child_session_id"]
        return None
    plugin_ctx.register_hook("subagent_start", on_start)

    # Read the plugin summary BEFORE unload: the command handler is gone
    # afterwards, and a missing expected row is a runner failure, not
    # "unobserved".
    def shadow_row():
        handler = get_plugin_command_handler("subagent_effort")
        if handler is None:
            raise SystemExit("subagent_effort command handler missing after delegation")
        payload = json.loads(handler())
        rows = {row["child_session_id"]: row for row in payload.get("children", [])}
        sid = telemetry["child_session_id"]
        if sid is None or sid not in rows:
            raise SystemExit(f"expected shadow row for child {sid!r} missing "
                             f"(have {sorted(rows)})")
        row = rows[sid]
        # classification_state may be pending/unavailable: the row itself and
        # its native usage/origin/cost are real child evidence regardless;
        # never fabricate a verdict and never wait for classification
        state = row.get("classification_state") or (
            "classified" if row.get("verdict") is not None else "unavailable")
        if state != "classified":
            print(f"subagent-effort shadow row {sid}: classification not "
                  f"complete ({state}); native evidence retained",
                  file=sys.stderr)
        row.setdefault("classification_state", state)
        return {"row": row, "payload": payload}

    from run_agent import AIAgent
    from hermes_constants import parse_reasoning_effort
    parent = AIAgent(
        base_url=args.base_url, api_key=os.environ.get("LOCAL_MODEL_API_KEY") or "local",
        provider="lilith", api_mode="chat_completions", model=args.model,
        reasoning_config=parse_reasoning_effort(effort),
        # native seed for paired reproducibility; paired arms share a seed, repeats differ
        request_overrides={"max_tokens": 4096, "seed": seed},
        enabled_toolsets=["terminal", "file", "todo"],
        skip_background_review=True, skip_context_files=True, skip_memory=True,
        quiet_mode=True, platform="cli")
    # Observe the SDK's final serialized HTTP body, not its requested config.
    # No URL, headers, prompt, response content or credentials are persisted;
    # seed and effort are safe request metadata. HTTP acceptance is NOT proof
    # the backend honored an effort internally.
    import httpx
    from urllib.parse import urlsplit
    endpoint = urlsplit(args.base_url)
    wire = {"requested_effort": effort, "requests": [],
            "backend_effective_effort": "unknown"}
    send = httpx.Client.send
    async_send = httpx.AsyncClient.send

    def observe(http_request):
        url = http_request.url
        if (url.host != endpoint.hostname or url.port != endpoint.port
                or not url.path.endswith(("/chat/completions", "/responses"))):
            return None
        row = {"sequence": len(wire["requests"]), "actually_sent_effort": None,
               "status_code": None, "seed": None}
        body = None
        try:
            body = json.loads(http_request.content)
            values, sources = [], []
            if isinstance(body.get("reasoning_effort"), str):
                values.append(body["reasoning_effort"])
                sources.append("reasoning_effort")
            if isinstance((body.get("reasoning") or {}).get("effort"), str):
                values.append((body.get("reasoning") or {}).get("effort"))
                sources.append("reasoning.effort")
            if isinstance((body.get("chat_template_kwargs") or {}).get("reasoning_effort"), str):
                values.append((body.get("chat_template_kwargs") or {}).get("reasoning_effort"))
                sources.append("chat_template_kwargs.reasoning_effort")
            row["effort_sources"] = sources
            row["sent_effort_raw"] = values[0] if values else None
            row["effort_encoding_conflict"] = bool(len(set(values)) > 1)
            row["actually_sent_effort"] = values[0] if len(set(values)) == 1 \
                and values[0] in ALLOWED_EFFORTS else None
        except (ValueError, TypeError, AttributeError, httpx.RequestNotRead):
            row["metadata_unreadable"] = True
        if isinstance(body, dict):
            # provider-specific transmitted controls (safe integers/booleans):
            # Lilith maps effort to thinking_budget/chat_template_kwargs/thinking_scaffold
            row["thinking_budget"] = body.get("thinking_budget")
            row["thinking_scaffold"] = (body.get("chat_template_kwargs") or {}).get("thinking_scaffold")
            # safe metadata only: seed is an integer defined by the backend contract
            if isinstance(body.get("seed"), int):
                row["seed"] = body["seed"]
        wire["requests"].append(row)
        atomic(Path(args.output) / "wire.json", wire)
        return row

    def observed_send(client, http_request, **kwargs):
        row = observe(http_request)
        try:
            response = send(client, http_request, **kwargs)
            if row is not None:
                row["status_code"] = response.status_code
            return response
        except Exception as exc:
            if row is not None:
                row["transport_error_type"] = type(exc).__name__
            raise
        finally:
            if row is not None:
                atomic(Path(args.output) / "wire.json", wire)

    async def observed_async_send(client, http_request, **kwargs):
        row = observe(http_request)
        try:
            response = await async_send(client, http_request, **kwargs)
            if row is not None:
                row["status_code"] = response.status_code
            return response
        except Exception as exc:
            if row is not None:
                row["transport_error_type"] = type(exc).__name__
            raise
        finally:
            if row is not None:
                atomic(Path(args.output) / "wire.json", wire)

    atomic(Path(args.output) / "wire.json", wire)
    httpx.Client.send = observed_send
    httpx.AsyncClient.send = observed_async_send
    record_session_cwd(None, str(workspace))
    set_session_cwd(str(workspace))  # native explicit context pin
    try:
        from tools.delegate_tool import delegate_task
        result = json.loads(delegate_task(
            goal=request["goal"] + WORKFLOW,
            context="Work only in the current directory.",
            max_iterations=None, background=False, parent_agent=parent))
        entry = (result.get("results") or [{}])[0]
        entry.pop("_child_role", None)
        entry.pop("_child_cost_usd", None)
        if entry.get("cost_status") == "unknown":
            entry["cost_usd"] = None  # unknown provenance: never present 0 as actual
        shadow = shadow_row()
    finally:
        httpx.Client.send = send
        httpx.AsyncClient.send = async_send
        clear_session_cwd(None)
        clear_runtime_cwd()
        # manager.unload(None) also removes observer registrations.
        parent.close()
        manager.unload()
    telemetry.pop("_prev_hashes", None)
    telemetry.pop("_failed_pending", None)
    observed_evidence = [event["verification_evidence_status"]
                         for event in telemetry["tool_events"]
                         if event["verification_evidence_status"] in {"passed", "failed"}]
    telemetry["verification_ledger_rows_observed"] = len(observed_evidence)
    row = shadow["row"]
    atomic(Path(args.output) / "native.json", {
        "native": entry, "status": entry.get("status"),
        "requested_effort": effort,
        "requested_seed": seed,
        "actually_sent_efforts": sorted({r["actually_sent_effort"] for r in wire["requests"]
                                        if r["actually_sent_effort"] is not None}),
        "classifier": {k: row.get(k) for k in
                       ("verdict", "suggested_effort", "clamped_effort",
                        "supported_efforts", "child_reasoning_config", "child_model",
                        "child_provider", "outcome_status", "latency_ms",
                        "classification_state", "classification_reason")}
        | {"child_session_id": telemetry["child_session_id"]},
        "plugin_state": shadow["payload"]})
    atomic(Path(args.output) / "telemetry.json", telemetry)
    return 0


def service_identity(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            raw = response.read(65536)
        return {"url": url, "status": response.status, "identity": json.loads(raw or b"{}")}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"url": url, "error": redact(str(exc))[:500]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Enable real model/classifier calls")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--env-file", type=Path, default=Path("~/.hermes/.env"))
    parser.add_argument("--seed", type=int, default=20261009)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if not args.run:
            parser.error("--worker requires --run")
        return run_native(args, json.load(sys.stdin))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("--output must be empty; existing evidence is never overwritten")
    corpus_path = Path(__file__).with_name("shadow_cases.json")
    cases = json.loads(corpus_path.read_text(encoding="utf-8"))["cases"]
    if len(cases) != 6 or len({case["id"] for case in cases}) != 6:
        raise ValueError("Expected the frozen six-case corpus")
    random.Random(args.seed).shuffle(cases)
    manifest = {"schema_version": 1, "mode": "live" if args.run else "offline",
                "corpus_sha256": digest(corpus_path), "runner_sha256": digest(__file__),
                "plugin_sha256": digest(Path(__file__).resolve().parent.parent /
                                        "plugins" / "subagent-effort" / "__init__.py"),
                "hermes_python": sys.executable,
                "seed": args.seed, "order": [case["id"] for case in cases],
                "settings": {"model": args.model, "base_url": args.base_url,
                             "provider": "lilith", "api_mode": "chat_completions",
                             "effort": "xhigh", "max_iterations": 12, "child_timeout_seconds": 180,
                             "worker_timeout_seconds": 240, "assert_timeout_seconds": 20,
                             "max_tokens": 4096, "verify_on_stop": True,
                             "max_verify_nudges": 2, "subagent_auto_approve": False,
                             "background_reviews": False},
                "automatic_routing": "NOT_QUALIFIED"}
    if args.run:
        # Health identity is recorded BEFORE any expensive worker/model call.
        manifest["services"] = {
            "model": service_identity(args.base_url.rstrip("/") + "/models"),
            "classifier": service_identity(CLASSIFIER + "/health"),
        }
    atomic(output / "manifest.json", manifest)
    results = []
    with tempfile.TemporaryDirectory(prefix="shadow-outcomes-") as root:
        workspaces = {}
        # Every initial baseline is saved BEFORE any model or classifier call.
        for case in cases:
            workspace = Path(root) / case["id"] / "workspace"
            public_hash = materialize(case, workspace)
            workspaces[case["id"]] = workspace
            result = {"id": case["id"], "status": "baseline",
                      "public_test_initial_sha256": public_hash,
                      "initial_source_hashes": source_hashes(workspace),
                      "baseline": {"public": check(case["public_check"], workspace),
                                   "heldout": check(case["heldout_assert_script"], workspace)}}
            result["baseline"]["proven_broken"] = (
                result["baseline"]["public"]["assertion_failure"]
                and result["baseline"]["heldout"]["expected_failure"])
            results.append(result)
            atomic(output / case["id"] / "result.json", result)
            atomic(output / "results.json",
                   {"results": results, "aggregate": aggregate(results, mode="offline")})
        manifest["baselines_proven_broken"] = all(r["baseline"]["proven_broken"] for r in results)
        atomic(output / "manifest.json", manifest)
        if args.run and manifest["baselines_proven_broken"]:
            for case, result in zip(cases, results):
                workspace = workspaces[case["id"]]
                case_output = output / case["id"]
                result["status"] = "running"
                atomic(case_output / "result.json", result)
                atomic(output / "results.json",
                       {"results": results, "aggregate": aggregate(results, mode="live")})
                worker_env = clean_env()
                worker_env["HOME"] = str(workspace.parent / "user-home")
                Path(worker_env["HOME"]).mkdir()
                request = {"goal": case["user_goal"], "workspace": str(workspace),
                           "hermes_home": str(workspace.parent / "hermes-home")}
                try:
                    worker = bounded([sys.executable, str(Path(__file__).resolve()),
                                      "--run", "--worker",
                                      "--output", str(case_output), "--model", args.model,
                                      "--base-url", args.base_url,
                                      "--env-file", str(args.env_file.expanduser().resolve())],
                                     workspace, 240, env=worker_env, stdin=json.dumps(request))
                    result["worker"] = worker
                    native_path = case_output / "native.json"
                    if native_path.exists():
                        result.update(json.loads(native_path.read_text()))
                    else:
                        result["status"] = "worker_timeout" if worker["timed_out"] else "worker_failed"
                    trace_path = case_output / "telemetry.json"
                    if trace_path.exists():
                        result["telemetry"] = json.loads(trace_path.read_text())
                except Exception as exc:
                    result.update(status="runner_error", error=redact(str(exc))[:LIMIT])
                # Provider/setup rejections are infrastructure failures, not
                # coding outcomes, and prevent evaluation completion. Retain
                # bounded-task outcomes (timeout/max_iterations) for scoring.
                native = result.get("native")
                worker_infra = result.get("status") in {"worker_timeout", "worker_failed", "runner_error"}
                if native is None or worker_infra:
                    result["infra_error"] = True
                    result["evaluable"] = False
                    result["correct"] = None
                elif native.get("status") in {"failed", "error"} and native.get("exit_reason") == "error":
                    result["infra_error"] = True
                    result["evaluable"] = False
                    result["correct"] = None
                else:
                    result["evaluable"] = True
                result["final_source_hashes"] = source_hashes(workspace)
                result["public_test_final_sha256"] = digest(workspace / "test_public.py")
                result["public_test_unchanged"] = bool(result["public_test_final_sha256"]
                                                       and result["public_test_final_sha256"]
                                                       == result["public_test_initial_sha256"])
                # Freeze source bytes before independent assertions; do not
                # expose the held-out oracle as a file the child could read.
                snapshot = case_output / "sources"
                snapshot.mkdir(exist_ok=True)
                for name in result["final_source_hashes"]:
                    target = snapshot / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    path = workspace / name
                    if path.is_file() and not path.is_symlink():
                        target.write_bytes(path.read_bytes())
                result["public"] = check(case["public_check"], workspace)
                result["heldout"] = check(case["heldout_assert_script"], workspace)
                if not result.get("infra_error"):
                    result["correct"] = (result["heldout"]["passed"] and result["public"]["passed"]
                                         and result["public_test_unchanged"])
                atomic(case_output / "result.json", result)
                atomic(output / "results.json",
                       {"results": results, "aggregate": aggregate(results, mode="live")})
        elif args.run:
            manifest["run_blocked"] = ("Initial assertion-failure baselines not established; "
                                       "no model calls made")
            atomic(output / "manifest.json", manifest)
    # exit 0 == evaluation COMPLETE and observed (outcomes may all be wrong;
    # bad model results are valid data). Infrastructure gaps exit 1.
    mode = "live" if args.run else "offline"
    agg = aggregate(results, mode=mode)
    manifest["baselines_complete"] = manifest["baselines_proven_broken"]
    manifest["evaluation_complete"] = bool(agg["evaluation_complete"]) if args.run else False
    atomic(output / "manifest.json", manifest)
    print(json.dumps({"output": str(output),
                      "baselines_proven_broken": manifest["baselines_proven_broken"],
                      "evaluation_complete": manifest["evaluation_complete"],
                      "aggregate": agg}, indent=2))
    return 0 if manifest["baselines_complete"] and (not args.run or manifest["evaluation_complete"]) else 1


if __name__ == "__main__":
    sys.exit(main())
