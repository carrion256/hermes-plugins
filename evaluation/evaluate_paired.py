#!/usr/bin/env python3
"""Paired subagent-effort experiment runner. Offline unless --run is explicit.

Offline (default): python3 evaluation/evaluate_paired.py --output DIR
  Establishes per-case baselines (public assertion-failure AND heldout failure)
  with NO model/classifier calls.

Live:  add --run with --efforts low,xhigh --repeats 2.
  Each arm runs the same native Hermes worker as evaluate_shadow.py, with an
  explicit per-arm reasoning effort (the shared worker default stays xhigh).
  Automatic routing stays NOT_QUALIFIED unless the machine-readable evidence
  clears every promotion gate; see aggregate_pairs.

Frozen corpus/results are never touched: concurrency/cross-file cases are
referenced by id from evaluation/shadow_cases.json; missing categories come
from evaluation/paired_cases.json.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

# Reuse every helper verbatim: redaction, atomic writes, bounded execution,
# clean child env, workspace materialization, deterministic checks.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate_shadow import (ALLOWED_EFFORTS, BASE_URL, CLASSIFIER, MODEL,
                             atomic, bounded, check, clean_env, digest,
                             materialize, redact, source_hashes)

EVAL_SCRIPT = Path(__file__).resolve().parent / "evaluate_shadow.py"
DEFAULT_EFFORTS = ("medium", "xhigh")


def load_cases():
    """All frozen six cases + additive corpus, each with category metadata.

    Frozen bytes stay untouched (referenced by path, never rewritten): the
    runner is the sole source of category labels and per-case oracle-baseline
    expectations (public assertion-failure AND heldout failure), matching the
    frozen execution contract. Additive cases keep their own metadata.
    """
    shadow_path = Path(__file__).resolve().parent / "shadow_cases.json"
    paired_path = Path(__file__).resolve().parent / "paired_cases.json"
    frozen = json.loads(shadow_path.read_text(encoding="utf-8"))["cases"]
    additive = json.loads(paired_path.read_text(encoding="utf-8"))["cases"]
    if len(frozen) != 6 or len({c["id"] for c in frozen}) != 6:
        raise ValueError("Frozen six-case corpus changed; refusing to pair against it")
    if len({c["id"] for c in additive}) != len(additive):
        raise ValueError("Duplicate ids in additive corpus")
    overlap = {c["id"] for c in frozen} & {c["id"] for c in additive}
    if overlap:
        raise ValueError(f"Corpora overlap on {sorted(overlap)}; additive corpus must not redefine frozen cases")
    # frozen bytes are referenced, never rewritten: category labels and
    # baseline expectations for the frozen cases live here in the runner
    frozen_metadata = {
        "suffix-removal": ["string-handling"],
        "page-count": ["input-validation"],
        "record-lines": ["boundary-handling"],
        "memo-flight": ["concurrency"],
        "ledger-batches": ["cross-file", "state-mutation"],
        "catalog-csv": ["cross-file", "parser-CLI"],
    }
    if {c["id"] for c in frozen} != set(frozen_metadata):
        raise ValueError(
            "Frozen corpus ids changed; refusing to pair against it: "
            f"{sorted({c['id'] for c in frozen} ^ set(frozen_metadata))}")
    selected = [dict(c, categories=frozen_metadata[c["id"]]) for c in frozen]
    selected += additive
    expected_categories = {"concurrency", "cross-file", "dependency-api",
                           "migration-atomicity", "path-security", "control",
                           "string-handling", "input-validation",
                           "boundary-handling", "state-mutation", "parser-CLI"}
    present = set()
    for case in selected:
        present.update(case["categories"])
    if present != expected_categories:
        raise ValueError(f"Selection covers {sorted(present)}, missing {sorted(expected_categories - present)}")
    return selected, digest(shadow_path), digest(paired_path)


def trial_plan(cases, arms, repeats, seed):
    """Seeded initial order per case, reversed on alternate repetitions."""
    if len(arms) != 2 or len(set(arms)) != 2:
        raise ValueError("paired trials require two distinct efforts")
    plan = []
    for case in cases:
        first_order = list(arms)
        random.Random(f"{seed}:{case['id']}").shuffle(first_order)
        for repeat in range(1, repeats + 1):
            identity = f"{seed}:{case['id']}:{repeat}"
            model_seed = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:4], "big")
            order = first_order[:] if repeat % 2 else first_order[::-1]
            for position, arm in enumerate(order):
                plan.append({"case_id": case["id"], "repeat": repeat,
                             "arm": arm, "position": position,
                             "seed": model_seed, "trial_seed": identity,
                             "arm_order": order})
    return plan


def arm_layout(root, case_id, repeat, arm):
    """Per-trial isolated runtime layout.

    The runtime workspace is named with an opaque digest, not the effort
    level: HOME/HERMES_HOME/workspace all hang below it, and neither the
    model's cwd nor any sidecar directory leaks the arm identity.
    """
    digest = hashlib.sha256(f"{case_id}:{repeat}:{arm}".encode()).hexdigest()[:12]
    side = root / "t" / digest
    return {"side": side, "workspace": side / "workspace",
            "user_home": side / "user-home", "hermes_home": side / "hermes-home"}


def arm_evidence(record):
    """Classify one attempt's evidence WITHOUT trusting synthetic labels."""
    native = record.get("native") or {}
    wire = record.get("wire") or {}
    requests = wire.get("requests") or []
    requested = record.get("requested_effort")
    sent_ok = bool(requests) and all(
        r.get("actually_sent_effort") == requested for r in requests)
    coerced = any(r.get("sent_effort_raw") is not None
                  and r.get("actually_sent_effort") != requested for r in requests)
    field_absent = any(r.get("sent_effort_raw") is None
                       and r.get("actually_sent_effort") is None for r in requests)
    backend = {
        "observed_requests": len(requests),
        "effort_field_present_on_every_request": bool(requests) and all(
            r.get("actually_sent_effort") is not None for r in requests),
        "effort_sources_seen": sorted({src for r in requests
                                        for src in r.get("effort_sources", [])}),
        "encoding_conflicts": sum(1 for r in requests if r.get("effort_encoding_conflict")),
        "sent_effort_matches_requested": sent_ok,
        "coerced_raw_values": sorted({r["sent_effort_raw"] for r in requests
                                       if r.get("sent_effort_raw") is not None
                                       and r.get("actually_sent_effort") != requested}),
        "field_absent_requests": sum(
            1 for r in requests
            if r.get("sent_effort_raw") is None and r.get("actually_sent_effort") is None),
        "transport_errors": sum(1 for r in requests if "transport_error_type" in r),
        "metadata_unreadable": sum(1 for r in requests if r.get("metadata_unreadable")),
        "http_rejected": sum(1 for r in requests
                             if isinstance(r.get("status_code"), int) and r["status_code"] >= 400),
        # HTTP acceptance is NOT proof of backend support; only the wire field is
        # evidence the level reached the backend in the requested shape.
        "backend_effective_effort": "unknown",
    }
    if not requests:
        backend["support"] = "zero-observation"
    elif backend["transport_errors"] or backend["http_rejected"]:
        backend["support"] = "rejected"
    elif coerced or field_absent:
        backend["support"] = "unsupported"
    elif backend["sent_effort_matches_requested"]:
        backend["support"] = "accepted-on-wire"
    else:
        backend["support"] = "unsupported"
    infra = bool(record.get("infra_error")) or backend["support"] == "rejected"
    coding = native.get("status") in {"completed", "failed", "timeout"} and native.get("exit_reason") in {
        "completed", "max_iterations", "timeout"}
    native_cost_usd = native.get("cost_usd")
    native_cost_status = (native.get("cost_status")
                          if isinstance(native.get("cost_status"), str) and native.get("cost_status")
                          else "unknown")
    cost = {
        "cost_usd": native_cost_usd if isinstance(native_cost_usd, (int, float)) else None,
        "cost_status": native_cost_status,
        "measured": isinstance(native_cost_usd, (int, float)),
        # native accounting is never actual billing: report the status label
        # verbatim; unobserved/non-numeric values are explicit, never zeroed
        "provenance": ("native-reported" if isinstance(native_cost_usd, (int, float))
                       else "unobserved" if native_cost_usd is None
                       else "unvalidated"),
        "cost_source": native.get("cost_source"),
    }
    return ({"backend": backend, "infra_error": infra, "coding_outcome_known": coding,
             "cost": cost})


def aggregate_pairs(pairs, expected_arms, expected_pair_keys):
    """Pair by (case, repeat). Conservative gates; NEVER promotes on partial evidence.

    expected_arms/expected_pair_keys MUST come from the plan (declared design),
    never from observed data: an arm missing from every observation, a pair key
    never run, a duplicate observation, or an unrequested pair all abort here.

    pairs: list of {case_id, repeat, arms: {arm: result_record}}.
    """
    expected_arms = sorted(expected_arms)
    expected_pair_keys = {tuple(key) for key in expected_pair_keys}
    observed_keys = [(pair["case_id"], pair["repeat"]) for pair in pairs]
    duplicates = sorted({key for key in observed_keys if observed_keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"duplicate pair observations: {duplicates}")
    unrequested = sorted({key for key in observed_keys if key not in expected_pair_keys})
    if unrequested:
        raise ValueError(f"pairs outside the planned design: {unrequested}")
    complete, infra_errors, unpaired, unsupported, zero_obs = [], 0, [], [], []
    pairs_timeout = 0
    for pair_key in expected_pair_keys:
        observed = next((pair for pair in pairs
                         if (pair["case_id"], pair["repeat"]) == pair_key), None)
        if observed is None:
            unpaired.append(pair_key)
            continue
        arms = observed.get("arms", {})
        if set(arms) != set(expected_arms):
            unpaired.append(pair_key)
            continue
        classes = {arm: arm_evidence(arms[arm]) for arm in arms}
        if any(c["infra_error"] for c in classes.values()):
            infra_errors += 1
            continue
        if any(not c["coding_outcome_known"] for c in classes.values()):
            unpaired.append(pair_key)
            continue
        if any(c["backend"]["support"] == "unsupported" for c in classes.values()):
            unsupported.append(pair_key)
            continue
        if any(c["backend"]["support"] == "zero-observation" for c in classes.values()):
            zero_obs.append(pair_key)
            continue
        if any((arms[arm].get("native") or {}).get("exit_reason") == "timeout" for arm in arms):
            pairs_timeout += 1
        complete.append({**observed, "classes": classes})
    # per-arm resource and outcome summaries over COMPLETE pairs only
    arms_summary = {}
    for arm in expected_arms:
        rows = [p["arms"][arm] for p in complete]
        native_list = [r.get("native") or {} for r in rows]
        durations = [n["duration_seconds"] for n in native_list
                     if isinstance(n.get("duration_seconds"), (int, float))]
        tokens = [n["tokens"]["input"] + n["tokens"]["output"] for n in native_list
                  if isinstance((n.get("tokens") or {}).get("input"), (int, float))
                  and isinstance((n.get("tokens") or {}).get("output"), (int, float))]
        api_calls = [n["api_calls"] for n in native_list
                     if isinstance(n.get("api_calls"), int)]
        cost_measured = [p["classes"][arm]["cost"]["cost_usd"] for p in complete
                         if p["classes"][arm]["cost"]["measured"]]
        cost_provenance_counts = collections.Counter(
            p["classes"][arm]["cost"]["provenance"] for p in complete)
        # native accounting statuses pass through verbatim (estimated/partial/
        # unknown are never presented as actual billing)
        cost_status_counts = collections.Counter(
            p["classes"][arm]["cost"]["cost_status"] for p in complete)
        oracle = [r.get("oracle_assessment_seconds") for r in rows
                  if isinstance(r.get("oracle_assessment_seconds"), (int, float))]
        observed_corr = sum(1 for r in rows
                            if r.get("telemetry", {}).get("verification_ledger_rows_observed", 0) > 0)
        verification_rows = [r.get("telemetry", {}).get("verification_ledger_rows_observed")
                             for r in rows
                             if isinstance(r.get("telemetry", {}).get("verification_ledger_rows_observed"), int)]
        corrective_revisions = [r.get("telemetry", {}).get("verified_corrective_revisions")
                                for r in rows
                                if isinstance(r.get("telemetry", {}).get("verified_corrective_revisions"), int)]
        timeout_rows = [r for r in rows
                        if (r.get("native") or {}).get("exit_reason") == "timeout"]
        native_total = sum(durations) if durations else None
        oracle_total = sum(oracle) if oracle else None
        total_measured = (native_total + oracle_total
                          if native_total is not None and oracle_total is not None
                          else None)
        arms_summary[arm] = {
            "requested_effort": arm,
            "n": len(rows),
            "correct": sum(1 for r in rows if r.get("correct") is True),
            "incorrect": sum(1 for r in rows if r.get("correct") is False),
            "correctness_rate": (sum(1 for r in rows if r.get("correct") is True) / len(rows))
                                if rows else None,
            "native_execution_total_seconds": native_total,
            "oracle_assessment_total_seconds": oracle_total,
            "total_measured_seconds": total_measured,
            "latency_observations": len(durations),
            "latency_mean_seconds": statistics.mean(durations) if durations else None,
            "latency_uncertainty": {"type": "none", "value": None},
            "spread_note": "observed dispersion is descriptive spread, NOT a confidence interval",
            "tokens_total": sum(tokens) if tokens else None,
            "token_observations": len(tokens),
            "token_uncertainty": {"type": "none", "value": None},
            "api_calls_total": sum(api_calls) if api_calls else None,
            "api_call_observations": len(api_calls),
            "oracle_assessment_label": "deterministic-oracle-assessment (not model code review)",
            "public_passed": sum(1 for r in rows if (r.get("public") or {}).get("passed") is True),
            "public_observations": len(rows),
            "heldout_passed": sum(1 for r in rows if (r.get("heldout") or {}).get("passed") is True),
            "heldout_observations": len(rows),
            "timeouts": len(timeout_rows),
            "timeout_note": ("timeout duration is retained as a censored latency observation; "
                             "token/cost fields stay unobserved when the native worker returned none"),
            "verification_rows_total": sum(verification_rows) if verification_rows else None,
            "verification_row_observations": len(verification_rows),
            "verified_corrective_revisions_total": sum(corrective_revisions) if corrective_revisions else None,
            "corrective_revision_observations": len(corrective_revisions),
            "cost_usd_native_measured": sum(cost_measured) if cost_measured else None,
            "cost_usd_measured_observations": len(cost_measured),
            "cost_provenance_counts": dict(cost_provenance_counts),
            "cost_status_counts": dict(cost_status_counts),
            "cost_note": ("native accounting amounts are estimates/partial unless "
                          "reported actual; never summed into actual billing, never "
                          "routed on; unmeasured arms report null with provenance"),
            "cost_usd_actual": None,
            "cost_usd_observations": 0,
            "cost_provenance_bad": sum(
                1 for p in complete
                if p["classes"][arm]["cost"]["cost_status"] != "actual"
                or p["classes"][arm]["cost"]["provenance"] != "native-reported"),
            "verification_observable_arms": observed_corr,
            "repair_cycles": "UNKNOWN (0 verification-observable attempts)" if observed_corr == 0 else "observed",
        }
        # crude dispersion: pairwise spread is the uncertainty we can honestly report
        if durations and len(durations) >= 2:
            arms_summary[arm]["latency_uncertainty"] = {
                "type": "half-range", "value": (max(durations) - min(durations)) / 2}
        if tokens and len(tokens) >= 2:
            arms_summary[arm]["token_uncertainty"] = {
                "type": "half-range", "value": (max(tokens) - min(tokens)) / 2}
    # paired deltas (one arm vs the other), resource only
    paired_resource = []
    if len(expected_arms) == 2 and complete:
        a, b = expected_arms
        for metric in ("tokens", "durations"):
            deltas = []
            for pair in complete:
                na = pair["arms"][a].get("native") or {}
                nb = pair["arms"][b].get("native") or {}
                if metric == "tokens":
                    na_tok, nb_tok = na.get("tokens") or {}, nb.get("tokens") or {}
                    ok = (isinstance(na_tok.get("input"), (int, float))
                          and isinstance(na_tok.get("output"), (int, float))
                          and isinstance(nb_tok.get("input"), (int, float))
                          and isinstance(nb_tok.get("output"), (int, float)))
                    va = na_tok["input"] + na_tok["output"] if ok else None
                    vb = nb_tok["input"] + nb_tok["output"] if ok else None
                else:
                    va, vb = na.get("duration_seconds"), nb.get("duration_seconds")
                    ok = isinstance(va, (int, float)) and isinstance(vb, (int, float))
                if ok:
                    deltas.append(va - vb)
            if deltas:
                paired_resource.append({
                    "metric": metric, "a_minus_b": deltas,
                    "paired_mean_delta": statistics.mean(deltas),
                    "paired_delta_half_range": (max(deltas) - min(deltas)) / 2,
                    "n": len(deltas),
                    "interpretation": ("descriptive cluster statistics; observed spread "
                                       "is NOT a confidence interval; NOT a savings claim"),
                })
    qualification_gates = {
        "both_arms_present_in_every_pair": not unpaired,
        "no_infrastructure_errors": infra_errors == 0,
        "backend_accepted_on_wire": not unsupported,
        "nonzero_observations": not zero_obs,
        "actual_cost_observations_complete": all(
            arms_summary[arm]["cost_usd_observations"] == arms_summary[arm]["n"]
            and arms_summary[arm]["n"] > 0
            for arm in expected_arms),
        "repair_cycles_observable": all(
            arms_summary[arm]["verification_observable_arms"] > 0
            for arm in expected_arms),
    }
    # The actual-only gate fails honestly whenever billing is estimated/partial,
    # but it NEVER hides those measurements: per-arm cost_usd_native_measured,
    # cost_provenance_counts and cost_status_counts are always reported, and
    # neither estimates nor unknowns ever feed routing.
    # These two gates can NEVER pass in this study by design:
    # backend_effective_effort stays "unknown" (wire acceptance proves the field
    # reached the backend, not that the backend honored it), and a 10-case
    # purpose-made corpus cannot validate promotion policy. Routing therefore
    # remains NOT_QUALIFIED until a separate, real policy qualification study.
    qualification_gates["backend_effective_effort_known"] = False
    qualification_gates["promotion_policy_validated"] = False
    # automatic_routing is intentionally constant: this study may supply
    # evidence for a separate qualification process but never flips routing.
    automatic_routing = "NOT_QUALIFIED"
    return {
        "expected_arms": expected_arms,
        "expected_pair_keys": sorted(expected_pair_keys),
        "pairs_total": len(pairs), "pairs_complete": len(complete),
        "pairs_unpaired_or_incomplete": len(unpaired),
        "pairs_infra_error": infra_errors,
        "pairs_timeout": pairs_timeout,
        "pairs_unsupported_effort": len(unsupported),
        "pairs_zero_observation": len(zero_obs),
        "per_arm": arms_summary,
        "paired_resource": paired_resource,
        "automatic_routing": automatic_routing,
        "automatic_routing_note": ("constant by design; promotion requires a separate "
                                   "policy qualification study with resolved backend "
                                   "effective effort"),
        "qualification_gates": qualification_gates,
        "design": ("paired same-seed arms, counterbalanced order, byte-identical "
                   "starting state per pair; oracle assessment is deterministic and "
                   "labelled, never model code review"),
    }


def service_identity(url):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return {"url": url, "status": response.status,
                    "body": redact(response.read(2048).decode("utf-8", "replace"))[:2048]}
    except Exception as exc:
        return {"url": url, "error": redact(str(exc))[:500]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Enable real model/classifier calls")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--efforts", default="medium,xhigh",
                        help="exactly two distinct allowed efforts, comma separated")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--env-file", type=Path, default=Path("~/.hermes/.env"))
    parser.add_argument("--seed", type=int, default=20261010)
    args = parser.parse_args()
    arms = [a.strip() for a in args.efforts.split(",") if a.strip()]
    if len(arms) != 2 or len(set(arms)) != 2 or any(a not in ALLOWED_EFFORTS for a in arms):
        parser.error("--efforts must name exactly two distinct allowed efforts "
                     f"({', '.join(ALLOWED_EFFORTS)})")
    if args.repeats < 2:
        parser.error("--repeats must be at least 2")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("--output must be empty; existing evidence is never overwritten")

    cases, frozen_sha, additive_sha = load_cases()
    manifest = {"schema_version": 1, "mode": "live" if args.run else "offline",
                "frozen_corpus_sha256": frozen_sha, "additive_corpus_sha256": additive_sha,
                "runner_sha256": digest(__file__), "worker_sha256": digest(EVAL_SCRIPT),
                "plugin_sha256": digest(EVAL_SCRIPT.resolve().parent.parent /
                                        "plugins" / "subagent-effort" / "__init__.py"),
                "hermes_python": sys.executable,
                "efforts": arms, "default_arms": list(DEFAULT_EFFORTS),
                "cases": [c["id"] for c in cases],
                "settings": {"model": args.model, "base_url": args.base_url,
                             "provider": "lilith", "api_mode": "chat_completions",
                             "worker_default_effort": "xhigh", "max_iterations": 12,
                             "child_timeout_seconds": 180, "worker_timeout_seconds": 240,
                             "assert_timeout_seconds": 20, "max_tokens": 4096},
                "automatic_routing": "NOT_QUALIFIED",
                "oracle_assessment_label": "deterministic-oracle-assessment (not model code review)"}
    plan = trial_plan(cases, arms, args.repeats, args.seed)
    manifest["plan"] = plan
    atomic(output / "manifest.json", manifest)

    pairs = []
    with tempfile.TemporaryDirectory(prefix="paired-") as root:
        root = Path(root)
        baselines = []
        for case in cases:
            workspace = root / case["id"] / "baseline"
            public_hash = materialize(case, workspace)
            baseline = {"id": case["id"], "public_test_initial_sha256": public_hash,
                        "initial_source_hashes": source_hashes(workspace),
                        "baseline": {"public": check(case["public_check"], workspace),
                                     "heldout": check(case["heldout_assert_script"], workspace)}}
            baseline["baseline"]["proven_broken"] = (
                baseline["baseline"]["public"]["assertion_failure"]
                and baseline["baseline"]["heldout"]["expected_failure"])
            baselines.append(baseline)
            atomic(output / "baselines" / f"{case['id']}.json", baseline)
        manifest["baselines_proven_broken"] = all(b["baseline"]["proven_broken"] for b in baselines)
        atomic(output / "manifest.json", manifest)

        if args.run and manifest["baselines_proven_broken"]:
            manifest["services"] = {"model": service_identity(args.base_url.rstrip("/") + "/models"),
                                    "classifier": service_identity(CLASSIFIER + "/health")}
            atomic(output / "manifest.json", manifest)
            for step, trial in enumerate(plan):
                case = next(c for c in cases if c["id"] == trial["case_id"])
                pair_dir = output / case["id"] / f"r{trial['repeat']}" / trial["arm"]
                layout = arm_layout(root, trial["case_id"], trial["repeat"], trial["arm"])
                workspace = layout["workspace"]
                materialize(case, workspace)  # same starting bytes for every arm
                starting = source_hashes(workspace)
                starting_public = digest(workspace / "test_public.py")
                record = {"case_id": trial["case_id"], "repeat": trial["repeat"],
                          "arm": trial["arm"], "trial_seed": trial["trial_seed"],
                          "arm_order": trial["arm_order"], "position": trial["position"],
                          "requested_effort": trial["arm"],
                          "requested_seed": trial["seed"],
                          "arm_layout": {k: str(v) for k, v in layout.items()},
                          "starting_source_hashes": starting}
                # per-arm sidecars (layout above) keep HOME and HERMES_HOME
                # independent per trial; sharing them leaked first-arm state
                # into the second arm (live-run FileExistsError).
                worker_env = clean_env()
                worker_env["HOME"] = str(layout["user_home"])
                layout["user_home"].mkdir(parents=True)
                request = {"goal": case["user_goal"], "workspace": str(workspace),
                           "hermes_home": str(layout["hermes_home"]),
                           "effort": trial["arm"], "seed": trial["seed"]}
                try:
                    worker = bounded([sys.executable, str(EVAL_SCRIPT),
                                      "--run", "--worker",
                                      "--output", str(pair_dir), "--model", args.model,
                                      "--base-url", args.base_url,
                                      "--env-file", str(args.env_file.expanduser().resolve())],
                                     workspace, 240, env=worker_env, stdin=json.dumps(request))
                except Exception as exc:
                    worker = {"error": redact(str(exc))[:500], "timed_out": False,
                              "returncode": None}
                record["worker"] = worker
                native_path = pair_dir / "native.json"
                wire_path = pair_dir / "wire.json"
                telemetry_path = pair_dir / "telemetry.json"
                if native_path.exists():
                    record.update(json.loads(native_path.read_text()))
                else:
                    record["status"] = "worker_timeout" if worker["timed_out"] else "worker_failed"
                if wire_path.exists():
                    record["wire"] = json.loads(wire_path.read_text())
                if telemetry_path.exists():
                    record["telemetry"] = json.loads(telemetry_path.read_text())
                native = record.get("native")
                worker_infra = record.get("status") in {"worker_timeout", "worker_failed", "runner_error"}
                if native is None or worker_infra or (
                        native.get("status") in {"failed", "error"}
                        and native.get("exit_reason") == "error"):
                    record["infra_error"] = True
                    record["evaluable"] = False
                    record["correct"] = None
                else:
                    record["evaluable"] = True
                record["final_source_hashes"] = source_hashes(workspace)
                record["public_test_initial_sha256"] = starting_public
                record["public_test_unchanged"] = (digest(workspace / "test_public.py")
                                                   == starting_public)
                # freeze candidate bytes; heldout script never lives in the workspace
                snapshot = pair_dir / "sources"
                snapshot.mkdir(exist_ok=True)
                for name in record["final_source_hashes"]:
                    target = snapshot / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    path = workspace / name
                    if path.is_file() and not path.is_symlink():
                        target.write_bytes(path.read_bytes())
                oracle_start = time.monotonic()
                record["public"] = check(case["public_check"], workspace)
                record["heldout"] = check(case["heldout_assert_script"], workspace)
                record["oracle_assessment_seconds"] = round(time.monotonic() - oracle_start, 3)
                record["oracle_assessment_label"] = ("deterministic-oracle-assessment "
                                                     "(not model code review)")
                if not record.get("infra_error"):
                    record["correct"] = (record["heldout"]["passed"] and record["public"]["passed"]
                                         and record["public_test_unchanged"])
                atomic(pair_dir / "result.json", record)
                for existing in pairs:
                    if existing["case_id"] == trial["case_id"] and existing["repeat"] == trial["repeat"]:
                        existing["arms"][trial["arm"]] = record
                        break
                else:
                    pairs.append({"case_id": trial["case_id"], "repeat": trial["repeat"],
                                  "arms": {trial["arm"]: record}})
                atomic(output / "pairs.json",
                       {"pairs": pairs,
                        "aggregate": aggregate_pairs(
                            pairs, arms,
                            {(t["case_id"], t["repeat"]) for t in plan})})
        elif args.run:
            manifest["run_blocked"] = ("Initial assertion-failure/heldout baselines not "
                                       "established; no model calls made")
            atomic(output / "manifest.json", manifest)
    aggregate = aggregate_pairs(pairs, arms,
                                {(t["case_id"], t["repeat"]) for t in plan}) \
        if args.run and manifest["baselines_proven_broken"] \
        else {"pairs_total": 0, "pairs_complete": 0, "per_arm": {},
              "automatic_routing": "NOT_QUALIFIED",
              "run_blocked": manifest.get("run_blocked", "offline baselines only")}
    manifest["automatic_routing"] = aggregate["automatic_routing"]
    atomic(output / "manifest.json", manifest)
    print(json.dumps({"output": str(output),
                      "baselines_proven_broken": manifest["baselines_proven_broken"],
                      "aggregate": aggregate}, indent=2))
    if not args.run:
        return 0 if manifest["baselines_proven_broken"] else 1
    return 0 if manifest["baselines_proven_broken"] else 1


if __name__ == "__main__":
    sys.exit(main())
