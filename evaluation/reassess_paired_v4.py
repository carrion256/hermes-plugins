#!/usr/bin/env python3
"""Retrospective oracle re-assessment of retained paired-v4 candidate bytes.

The original paired-live-v4 used an invalid path-security oracle and an
incomplete schema-rollback oracle. The corpus oracles have since been
repaired. This script RE-RUNS ONLY the deterministic assessment of frozen
candidate sources under the
corrected oracles. It:

  - makes no model/network calls (offline, deterministic checks only);
  - never modifies the source evidence tree or the historical result.json;
  - copies candidate bytes into fresh owned temporary workspaces per arm;
  - records supersession/erratum metadata, frozen-source hash verification,
    and original native/wire/telemetry provenance alongside new outcomes;
  - keeps automatic routing NOT_QUALIFIED and the backend-effective-effort
    and cost-provenance unknowns intact.

Usage:
  python3 evaluation/reassess_paired_v4.py \
      --source-dir /tmp/hermes-plugin-upgrade-65g1f7cz/paired-live-v4 \
      --output-dir /tmp/hermes-plugin-upgrade-65g1f7cz/paired-v4-reassessment
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate_paired import DEFAULT_EFFORTS, aggregate_pairs, load_cases
from evaluate_shadow import atomic, check, digest, materialize, source_hashes

REPEATS = (1, 2)
ERRATUM_REASON = (
    "Original v4 oracle assessment for asset-fetch-boundary was invalid: the "
    "heldout preamble called fetch_asset(base, 'sentinel.txt') (nonexistent "
    "inside base) and caught only AssertionError, so correct implementations "
    "raising ValueError('invalid path') failed the oracle before its security "
    "assertions; the public check also probed /etc/shadow outside any owned "
    "sandbox. Oracles repaired in evaluation/paired_cases.json (owned "
    "TemporaryDirectory probes only, audit-hook escape detection, reference "
    "positive controls added). The migration oracle now checks DDL before "
    "DML in a failed run, detecting implicit transactions that preserve "
    "schema changes. The original frozen corpus remains untouched."
)


def reassess_arm(case, arm_dir, workspace_root):
    """Re-assess one retained arm; source tree is read-only."""
    record_path = arm_dir / "result.json"
    original = json.loads(record_path.read_text(encoding="utf-8"))
    snapshot = arm_dir / "sources"
    if not snapshot.is_dir() or not any(snapshot.rglob("*.py")):
        raise ValueError(f"{arm_dir}: no frozen candidate sources to reassess")
    workspace = workspace_root / f"{case['id']}-r{original['repeat']}-{original['arm']}"
    workspace.mkdir(parents=True)
    for name, content in case["initial_files"].items():
        # non-Python fixture bytes (e.g. app.yaml) were never snapshotted by
        # the v4 runner; materializing from the corpus reproduces the same
        # bytes the worker saw. Python fix-files must come from the snapshot.
        if name.endswith(".py"):
            source = snapshot / name
            if not source.is_file():
                raise ValueError(f"{arm_dir}: frozen source {name} missing")
            shutil.copyfile(source, workspace / name)
        else:
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    (workspace / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["test_public.py"]\n', encoding="utf-8")

    frozen_hashes = source_hashes(workspace)
    match = {name: frozen_hashes.get(name) == original_hash
             for name, original_hash in original["final_source_hashes"].items()}
    if set(match) != set(frozen_hashes) or not all(match.values()):
        raise ValueError(f"{arm_dir}: frozen candidate bytes changed "
                         f"(mismatched/extra: {sorted(n for n, ok in match.items() if not ok)})")

    start = time.monotonic()
    public = check(case["public_check"], workspace)
    heldout = check(case["heldout_assert_script"], workspace)
    elapsed = round(time.monotonic() - start, 3)

    new = {
        "case_id": original["case_id"],
        "repeat": original["repeat"],
        "arm": original["arm"],
        "requested_effort": original["requested_effort"],
        "trial_seed": original.get("trial_seed"),
        "public": public,
        "heldout": heldout,
        "oracle_assessment_seconds": elapsed,
        "oracle_assessment_label": ("deterministic-oracle-assessment "
                                    "(not model code review)"),
        # public_test_unchanged measured live in v4 and test_public.py is not
        # part of the frozen source snapshot; carry the original observation.
        "public_test_unchanged": original.get("public_test_unchanged", True),
        "correct": (public["passed"] and heldout["passed"]
                    and original.get("public_test_unchanged", True)),
        "original_correct_v4": original.get("correct"),
        "frozen_source_hashes": frozen_hashes,
        "frozen_source_match": match,
        # original provenance carried verbatim, never rewritten
        "native": original.get("native"),
        "wire": original.get("wire"),
        "telemetry": original.get("telemetry"),
        "worker": {k: original.get("worker", {}).get(k)
                   for k in ("returncode", "timed_out", "duration_seconds")},
        "status": original.get("status"),
        "infra_error": original.get("infra_error", False),
        "evaluable": original.get("evaluable", True),
        "provenance": {
            "source_result_sha256": digest(record_path),
            "source_native_sha256": digest(arm_dir / "native.json"),
            "source_wire_sha256": digest(arm_dir / "wire.json"),
            "source_telemetry_sha256": digest(arm_dir / "telemetry.json"),
            "source_candidate_sha256": frozen_hashes,
        },
    }
    return new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--efforts", default=",".join(DEFAULT_EFFORTS))
    args = parser.parse_args()

    arms = [a.strip() for a in args.efforts.split(",") if a.strip()]
    source = args.source_dir.resolve()
    output = args.output_dir.resolve()
    if output == source or source == output.parent or output.is_relative_to(source):
        parser.error("--output-dir must be separate from --source-dir; "
                     "historical evidence is never written into")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("--output-dir must be empty; existing evidence is never overwritten")

    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    cases, frozen_sha, additive_sha = load_cases()
    plan_keys = {(t["case_id"], t["repeat"]) for t in manifest["plan"]}

    pairs = {}
    with tempfile.TemporaryDirectory(prefix="reassess-") as root:
        root = Path(root)
        for case in cases:
            for repeat in REPEATS:
                for arm in arms:
                    arm_dir = source / case["id"] / f"r{repeat}" / arm
                    if not (arm_dir / "result.json").is_file():
                        continue  # arm never ran: accounted as unpaired below
                    new = reassess_arm(case, arm_dir, root)
                    pair = pairs.setdefault(
                        (case["id"], repeat),
                        {"case_id": case["id"], "repeat": repeat, "arms": {}})
                    pair["arms"][arm] = new
                    atomic(output / case["id"] / f"r{repeat}" / arm / "result.json", new)

    observed_keys = set(pairs)
    pairs_list = list(pairs.values())
    aggregate = aggregate_pairs(pairs_list, arms, plan_keys)
    aggregate["mode"] = "retrospective-reassessment"
    aggregate["automatic_routing"] = "NOT_QUALIFIED"
    aggregate["automatic_routing_note"] = (
        "constant by design; backend_effective_effort stays unknown and the "
        "6-case purpose-made corpus cannot validate promotion policy")

    erratum = {
        "supersedes": str(source / "pairs.json"),
        "supersedes_mode": "preregistered-original-assessment",
        "this_assessment_mode": "retrospective-erratum",
        "is_preregistered_rerun": False,
        "invalid_original_assessment_reason": ERRATUM_REASON,
        "invalidated_cases": ["asset-fetch-boundary", "sqlite-schema-migration"],
        "corrected_corpus": {
            "frozen_corpus_sha256": frozen_sha,
            "additive_corpus_sha256": additive_sha,
            "original_additive_corpus_sha256": manifest.get("additive_corpus_sha256"),
            "oracle_repair_validated_by": "tests/test_evaluation_paired.py",
        },
        "source_tree_immutable": True,
        "arm_status_counts": {
            arm: sum(1 for p in pairs_list for v in p["arms"].values() if v["arm"] == arm)
            for arm in arms},
        "total_arms_observed": sum(len(p["arms"]) for p in pairs_list),
        "unobserved_pair_keys": [list(key) for key in sorted(plan_keys - observed_keys)],
    }

    atomic(output / "pairs.json", {"pairs": pairs_list, "aggregate": aggregate})
    atomic(output / "erratum.json", erratum)
    print(json.dumps({"output": str(output),
                      "arms_observed": erratum["total_arms_observed"],
                      "aggregate": aggregate}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
