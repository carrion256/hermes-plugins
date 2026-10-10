"""Offline gates for the paired-effort evaluator.

These tests never call models or the network: they validate corpus shape,
determinism of the counterbalanced plan, and that the aggregator refuses to
qualify automatic routing on incomplete, duplicated, infra-broken,
zero-observation or unknown-cost evidence. automatic_routing is NOT_QUALIFIED
by design in every scenario.
"""
import copy
import json
import hashlib
import pathlib
import sys
import tempfile

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
from evaluate_paired import (DEFAULT_EFFORTS, aggregate_pairs, arm_evidence,
                             arm_layout, load_cases, trial_plan)

CASES, _FROZEN_SHA, _ADDITIVE_SHA = load_cases()
ARMS = list(DEFAULT_EFFORTS)  # declared design default; API tests use it explicitly
SEED = 20261010
PAIR_KEYS = {(c["id"], r) for c in CASES for r in (1, 2)}
FROZEN_IDS = {"suffix-removal", "page-count", "record-lines", "memo-flight",
              "ledger-batches", "catalog-csv"}
ADDITIVE_IDS = {"yaml-service-config", "sqlite-schema-migration",
                "asset-fetch-boundary", "slugify"}
EXPECTED_CATEGORIES = {"concurrency", "cross-file", "dependency-api",
                       "migration-atomicity", "path-security", "control",
                       "string-handling", "input-validation",
                       "boundary-handling", "state-mutation", "parser-CLI"}


def good_arm(arm, correct=True):
    return {
        "arm": arm, "requested_effort": arm, "correct": correct, "evaluable": True,
        "native": {"status": "completed", "exit_reason": "completed",
                   "duration_seconds": 12.0, "api_calls": 4,
                   "tokens": {"input": 100, "output": 50},
                   "cost_usd": 0.01, "cost_status": "actual"},
        "wire": {"requests": [{"sequence": 0, "sent_effort_raw": arm,
                               "actually_sent_effort": arm, "status_code": 200}]},
        "telemetry": {"verification_ledger_rows_observed": 1},
        "oracle_assessment_seconds": 0.2,
        "public_test_unchanged": True,
        "public": {"passed": True},
        "heldout": {"passed": True},
    }


def full_pairs():
    return [{"case_id": c["id"], "repeat": r,
             "arms": {arm: good_arm(arm) for arm in ARMS}}
            for c in CASES for r in (1, 2)]


def agg(pairs, arms=ARMS, pair_keys=PAIR_KEYS):
    return aggregate_pairs(pairs, arms, pair_keys)


def test_corpus_covers_categories_and_keeps_frozen_separate():
    additive = [c for c in CASES if c["id"] in ADDITIVE_IDS]
    frozen = [c for c in CASES if c["id"] in FROZEN_IDS]
    # all six frozen cases are paired now, not just two
    assert len(additive) == 4 and len(frozen) == 6 and len(CASES) == 10
    categories = set()
    for case in CASES:
        categories.update(case["categories"])
    assert categories == EXPECTED_CATEGORIES
    for case in CASES:
        assert case["categories"]
        assert case["initial_files"] and case["public_check"] and case["heldout_assert_script"]


def test_trial_plan_counterbalanced_deterministic():
    plan1 = trial_plan(CASES, ARMS, 2, SEED)
    plan2 = trial_plan(CASES, ARMS, 2, SEED)
    assert plan1 == plan2
    keys = {(t["case_id"], t["repeat"], t["arm"]) for t in plan1}
    assert len(keys) == len(plan1)
    # within-case counterbalancing: repeat 2 runs the reverse of repeat 1
    for case in CASES:
        order1 = [t for t in plan1 if t["case_id"] == case["id"] and t["repeat"] == 1]
        order2 = [t for t in plan1 if t["case_id"] == case["id"] and t["repeat"] == 2]
        assert len(order1) == len(order2) == 2
        first = sorted(order1, key=lambda t: t["position"])
        second = sorted(order2, key=lambda t: t["position"])
        assert [t["arm"] for t in second] == [t["arm"] for t in first][::-1]


def test_trial_seed_shared_within_trial_and_varies_per_repeat():
    plan = trial_plan(CASES, ARMS, 3, SEED)
    for case in CASES:
        for repeat in (1, 2, 3):
            rows = [t for t in plan if t["case_id"] == case["id"] and t["repeat"] == repeat]
            seeds = {t["seed"] for t in rows}
            assert len(seeds) == 1, "paired arms of one trial must share the numeric seed"
    # repeats transmit different numeric seeds
    case = CASES[0]
    seeds = trial_plan([case], ARMS, 3, SEED)
    assert {t["seed"] for t in seeds if t["repeat"] == 1} != \
        {t["seed"] for t in seeds if t["repeat"] == 2}
    # deterministic derivation from the trial identity
    expected = int.from_bytes(
        hashlib.sha256(f"{SEED}:{case['id']}:1".encode()).digest()[:4], "big")
    assert seeds[0]["seed"] == expected


def test_nondefault_requested_arms_aggregate():
    arms = ["low", "xhigh"]
    plan = trial_plan(CASES, arms, 2, SEED)
    assert {t["arm"] for t in plan} == {"low", "xhigh"}
    keyed = {}
    for t in plan:
        pair = keyed.setdefault((t["case_id"], t["repeat"]),
                                {"case_id": t["case_id"], "repeat": t["repeat"], "arms": {}})
        pair["arms"][t["arm"]] = good_arm(t["arm"])
    result = agg(list(keyed.values()), arms=arms,
                 pair_keys={(t["case_id"], t["repeat"]) for t in plan})
    assert result["expected_arms"] == ["low", "xhigh"]
    assert result["pairs_unpaired_or_incomplete"] == 0
    assert set(result["per_arm"]) == {"low", "xhigh"}
    assert "medium" not in result["per_arm"]


def test_lilith_transport_encoding_captured_and_supported():
    rows = [{"sequence": 0, "sent_effort_raw": "medium",
             "actually_sent_effort": "medium", "status_code": 200,
             "effort_sources": ["chat_template_kwargs.reasoning_effort"],
             "thinking_budget": 8192, "thinking_scaffold": False, "seed": 1234}]
    evidence = arm_evidence({"requested_effort": "medium", "infra_error": False,
                             "wire": {"requests": rows},
                             "native": {"status": "completed", "exit_reason": "completed"}})
    assert evidence["backend"]["support"] == "accepted-on-wire"
    assert evidence["backend"]["effort_sources_seen"] == \
        ["chat_template_kwargs.reasoning_effort"]


def test_encoding_conflict_is_not_accepted():
    rows = [{"sequence": 0, "sent_effort_raw": "medium",
             "actually_sent_effort": None, "effort_sources": [
                 "reasoning_effort", "chat_template_kwargs.reasoning_effort"],
             "effort_encoding_conflict": True, "status_code": 200}]
    evidence = arm_evidence({"requested_effort": "medium", "infra_error": False,
                             "wire": {"requests": rows},
                             "native": {"status": "completed", "exit_reason": "completed"}})
    assert evidence["backend"]["encoding_conflicts"] == 1
    assert evidence["backend"]["support"] == "unsupported"


def test_expected_arms_come_from_plan_not_observed_data():
    # an arm missing from EVERY pair cannot silently disappear from the denominator
    pairs = full_pairs()
    for p in pairs:
        del p["arms"][ARMS[1]]
    result = agg(pairs)
    assert result["automatic_routing"] == "NOT_QUALIFIED"
    assert result["expected_arms"] == sorted(ARMS)
    assert result["pairs_unpaired_or_incomplete"] == len(pairs)
    assert result["pairs_complete"] == 0


def test_duplicate_pair_arm_observation_rejected():
    pairs = full_pairs()
    dup = copy.deepcopy(pairs[0])
    dup["arms"][ARMS[1]] = good_arm(ARMS[1])  # second observation of same pair
    pairs.append(dup)
    with pytest.raises(ValueError, match="duplicate pair observations"):
        agg(pairs)


def test_unrequested_pair_rejected():
    pairs = full_pairs()
    pairs.append({"case_id": CASES[0]["id"], "repeat": 99,
                  "arms": {arm: good_arm(arm) for arm in ARMS}})
    with pytest.raises(ValueError, match="outside the planned design"):
        agg(pairs)


def test_infra_error_pair_never_qualifies():
    bad = good_arm(ARMS[1])
    bad["infra_error"] = True
    bad["native"] = {"status": "failed", "exit_reason": "error"}
    pairs = full_pairs()
    pairs[0]["arms"][ARMS[1]] = bad
    result = agg(pairs)
    assert result["automatic_routing"] == "NOT_QUALIFIED"
    assert result["pairs_infra_error"] >= 1


def test_unsupported_effort_never_qualifies():
    coerced = good_arm(ARMS[1])
    coerced["wire"]["requests"][0]["sent_effort_raw"] = "high"
    coerced["wire"]["requests"][0]["actually_sent_effort"] = None
    assert arm_evidence(coerced)["backend"]["support"] == "unsupported"
    pairs = full_pairs()
    pairs[0]["arms"][ARMS[1]] = coerced
    result = agg(pairs)
    assert result["automatic_routing"] == "NOT_QUALIFIED"
    assert result["pairs_unsupported_effort"] >= 1


def test_rejected_effort_is_infrastructure():
    rejected = good_arm(ARMS[0])
    rejected["wire"]["requests"][0]["status_code"] = 400
    evidence = arm_evidence(rejected)
    assert evidence["backend"]["support"] == "rejected"
    assert evidence["infra_error"] is True


def test_zero_observation_never_qualifies():
    empty = good_arm(ARMS[1])
    empty["wire"]["requests"] = []
    assert arm_evidence(empty)["backend"]["support"] == "zero-observation"
    pairs = full_pairs()
    pairs[0]["arms"][ARMS[1]] = empty
    result = agg(pairs)
    assert result["automatic_routing"] == "NOT_QUALIFIED"
    assert result["pairs_zero_observation"] >= 1


def test_unknown_or_estimated_cost_blocks_qualification():
    # numeric cost_usd with estimated provenance must not count as actual
    for status in ("unknown", "estimated"):
        pairs = full_pairs()
        for p in pairs:
            for arm in ARMS:
                p["arms"][arm]["native"]["cost_status"] = status
                p["arms"][arm]["native"]["cost_usd"] = 0.01
        result = agg(pairs)
        assert result["automatic_routing"] == "NOT_QUALIFIED"
        assert not result["qualification_gates"]["actual_cost_observations_complete"]
        for arm in ARMS:
            assert result["per_arm"][arm]["cost_usd_observations"] == 0
            assert result["per_arm"][arm]["cost_provenance_bad"] == len(pairs)


def test_timeout_arm_is_censored_but_not_erased():
    pairs = full_pairs()
    timed = pairs[0]["arms"][ARMS[1]]
    timed["native"].update({"status": "timeout", "exit_reason": "timeout",
                            "duration_seconds": 180.0})
    timed["native"].pop("tokens", None)
    timed["native"].pop("cost_usd", None)
    result = agg(pairs)
    assert result["pairs_timeout"] == 1
    assert result["pairs_complete"] == 2 * len(CASES)
    assert result["per_arm"][ARMS[1]]["timeouts"] == 1
    assert result["per_arm"][ARMS[1]]["correct"] == 2 * len(CASES)
    assert result["per_arm"][ARMS[1]]["token_observations"] == 2 * len(CASES) - 1
    assert result["per_arm"][ARMS[1]]["cost_usd_measured_observations"] == 2 * len(CASES) - 1


def test_verification_measurements_are_aggregated():
    pairs = full_pairs()
    for p in pairs:
        for arm in ARMS:
            p["arms"][arm]["telemetry"] = {
                "verification_ledger_rows_observed": 2,
                "verified_corrective_revisions": 1,
            }
    result = agg(pairs)
    for arm in ARMS:
        assert result["per_arm"][arm]["verification_rows_total"] == 4 * len(CASES)
        assert result["per_arm"][arm]["verified_corrective_revisions_total"] == 2 * len(CASES)


def test_routing_never_qualifies_even_with_perfect_evidence():
    result = agg(full_pairs())
    assert result["automatic_routing"] == "NOT_QUALIFIED"
    assert result["qualification_gates"]["backend_effective_effort_known"] is False
    assert result["qualification_gates"]["promotion_policy_validated"] is False
    assert result["per_arm"][ARMS[0]]["correct"] == 2 * len(CASES)  # data retained


def test_total_measured_seconds_includes_oracle_assessment():
    result = agg(full_pairs())
    arm = result["per_arm"][ARMS[0]]
    assert arm["native_execution_total_seconds"] is not None
    assert arm["oracle_assessment_total_seconds"] is not None
    assert arm["total_measured_seconds"] == pytest.approx(
        arm["native_execution_total_seconds"] + arm["oracle_assessment_total_seconds"])


def test_paired_arms_get_independent_sidecar_environments():
    # regression: shared HOME/HERMES_HOME under the case directory raised
    # FileExistsError on the second arm and leaked first-arm state into it
    with tempfile.TemporaryDirectory() as root:
        root = pathlib.Path(root)
        layout_a = arm_layout(root, "slugify", 1, "medium")
        layout_b = arm_layout(root, "slugify", 1, "xhigh")
        assert layout_a["side"] != layout_b["side"]
        for key in ("workspace", "user_home", "hermes_home"):
            assert layout_a[key] != layout_b[key]
            assert not layout_b[key].is_relative_to(layout_a["side"])
        for layout in (layout_a, layout_b):
            for key in ("workspace", "user_home", "hermes_home"):
                layout[key].mkdir(parents=True)
        (layout_a["workspace"] / "only_a.py").write_text("marker = 'a'\n")
        (layout_b["workspace"] / "only_b.py").write_text("marker = 'b'\n")
        assert (layout_a["workspace"] / "only_a.py").read_text() == "marker = 'a'\n"
        assert (layout_b["workspace"] / "only_a.py").exists() is False
        assert (layout_b["workspace"] / "only_b.py").read_text() == "marker = 'b'\n"


def test_arm_layout_opaque_and_deterministic():
    with tempfile.TemporaryDirectory() as root:
        root = pathlib.Path(root)
        layout = arm_layout(root, "slugify", 1, "xhigh")
        expected = hashlib.sha256(b"slugify:1:xhigh").hexdigest()[:12]
        assert layout["workspace"].name == "workspace"
        assert layout["side"].name == expected
        # effort identity never appears in the runtime-side directory names
        assert not any(arm in str(layout[key]) for arm in DEFAULT_EFFORTS
                       for key in ("side", "workspace", "user_home", "hermes_home"))


def test_paired_token_delta_requires_both_counters():
    # a row with output=None must be EXCLUDED, never summed as 0
    pairs = full_pairs()
    for i, p in enumerate(pairs):
        victim = p["arms"][ARMS[0]]
        if i == 0:
            victim["native"]["tokens"] = {"input": 100, "output": None}
    result = agg(pairs)
    row = next(r for r in result["paired_resource"] if r["metric"] == "tokens")
    assert row["n"] == len(pairs) - 1
    assert row["interpretation"] == (
        "descriptive cluster statistics; observed spread "
        "is NOT a confidence interval; NOT a savings claim")
    assert "two_sided_sign_test_p" not in row


def test_spread_reported_as_descriptive_not_inferential():
    pairs = full_pairs()
    pairs[0]["arms"][ARMS[1]]["native"]["duration_seconds"] = 30.0
    result = agg(pairs)
    unc = result["per_arm"][ARMS[1]]["latency_uncertainty"]
    assert unc["type"] == "half-range" and unc["value"] > 0
    assert result["per_arm"][ARMS[1]]["spread_note"] == (
        "observed dispersion is descriptive spread, NOT a confidence interval")


# reference (independent correct) implementations, authored from the user
# goals alone; the corpus oracle must pass them on BOTH public and heldout.
REFERENCE_IMPL = {
    "memo-flight": {
        "memo.py": "import threading\n\n_MISSING = object()\n\n\nclass Memo:\n    def __init__(self):\n        self.values = {}\n        self._locks = {}\n        self._meta = threading.Lock()\n\n    def get(self, key, factory):\n        # one lock per key: at most one factory runs per key, followers wait\n        # on it, and unrelated keys are never blocked by a busy one\n        with self._meta:\n            lock = self._locks.get(key)\n            if lock is None:\n                lock = threading.Lock()\n                self._locks[key] = lock\n        with lock:\n            value = self.values.get(key, _MISSING)\n            if value is _MISSING:\n                try:\n                    value = factory()\n                except BaseException:\n                    self.values.pop(key, None)\n                    raise\n                self.values[key] = value\n        return value\n"
    },
    "ledger-batches": {
        "ledger.py": "def apply_batch(state, events):\n    balances = state['balances']\n    seen = state['seen']\n    pending = {}\n    new_ids = set()\n    for event in events:\n        event_id = event['id']\n        if event_id in seen or event_id in new_ids:\n            continue\n        current = pending.get(event['account'], balances.get(event['account'], 0))\n        updated = current + event['delta']\n        if updated < 0:\n            raise ValueError('insufficient funds')\n        pending[event['account']] = updated\n        new_ids.add(event_id)\n    for account, updated in pending.items():\n        balances[account] = updated\n    seen.update(new_ids)\n    return state\n",
        "report.py": "def summarize(state):\n    return sorted((account, balance)\n                  for account, balance in state['balances'].items()\n                  if balance != 0)\n"
    },
    "yaml-service-config": {
        "service_config.py": "import yaml\n\n\ndef load_config(path):\n    with open(path, encoding=\"utf-8\") as stream:\n        try:\n            data = yaml.safe_load(stream)\n        except yaml.YAMLError:\n            raise ValueError(\"invalid config\")\n    if data is None:\n        return {}\n    if not isinstance(data, dict):\n        raise ValueError(\"invalid config\")\n    return data\n"
    },
    "sqlite-schema-migration": {
        "migrate.py": "import sqlite3\n\nMIGRATIONS = [\n    (2, \"ALTER TABLE accounts ADD COLUMN email TEXT\"),\n]\n\n\ndef migrate(db_path):\n    connection = sqlite3.connect(db_path, isolation_level=None)\n    try:\n        current = connection.execute(\n            \"SELECT version FROM schema_meta\").fetchone()[0]\n        pending = sorted((t, s) for t, s in MIGRATIONS if t > current)\n        if not pending:\n            return 0\n        connection.execute(\"BEGIN IMMEDIATE\")\n        try:\n            for target, sql in pending:\n                connection.execute(sql)\n            connection.execute(\n                \"UPDATE schema_meta SET version = ?\", (pending[-1][0],))\n        except BaseException:\n            connection.execute(\"ROLLBACK\")\n            raise\n        connection.execute(\"COMMIT\")\n        return len(pending)\n    finally:\n        connection.close()\n"
    },
    "asset-fetch-boundary": {
        "asset_fetch.py": "import os\n\n\ndef fetch_asset(base_dir, requested_path):\n    base = os.path.realpath(base_dir)\n    if os.path.isabs(requested_path):\n        raise ValueError(\"invalid path\")\n    target = os.path.realpath(os.path.join(base, requested_path))\n    if target != base and not target.startswith(base + os.sep):\n        raise ValueError(\"invalid path\")\n    if not os.path.isfile(target):\n        raise ValueError(\"invalid path\")\n    with open(target, \"rb\") as stream:\n        return stream.read()\n"
    },
    "slugify": {
        "slugify.py": "import re\n\n\ndef slugify(text):\n    return re.sub(r\"^-+|-+$\", \"\", re.sub(r\"[^a-z0-9]+\", \"-\", text.lower()))\n"
    },
    "suffix-removal": {
        "suffix.py": ("def remove_suffix(text, suffix):\n"
                      "    return text.removesuffix(suffix)\n"),
    },
    "page-count": {
        "pages.py": ("def page_count(total, size):\n"
                     "    return -(-total // size)\n"),
    },
    "record-lines": {
        "records.py": ("import json\n\n\ndef parse_records(text):\n"
                       "    rows = []\n"
                       "    for number, line in enumerate(text.splitlines(), 1):\n"
                       "        if not line.strip():\n"
                       "            continue\n"
                       "        try:\n"
                       "            value = json.loads(line)\n"
                       "        except ValueError:\n"
                       "            raise ValueError(f'line {number}: expected JSON object')\n"
                       "        if not isinstance(value, dict):\n"
                       "            raise ValueError(f'line {number}: expected JSON object')\n"
                       "        rows.append(value)\n"
                       "    return rows\n"),
    },
    "catalog-csv": {
        "catalog.py": (
            "import csv\n\n\n"
            "def read_catalog(stream):\n"
            "    reader = csv.reader(stream)\n"
            "    try:\n"
            "        header = next(reader)\n"
            "    except StopIteration:\n"
            "        raise ValueError('invalid catalog')\n"
            "    if header != ['name', 'quantity']:\n"
            "        raise ValueError('invalid catalog')\n"
            "    result = {}\n"
            "    try:\n"
            "        for row in reader:\n"
            "            if not row:\n"
            "                continue\n"
            "            if len(row) != 2:\n"
            "                raise ValueError('invalid catalog')\n"
            "            name, quantity = (row[0].strip(), row[1].strip())\n"
            "            if not name or not quantity.isascii() or not quantity.isdigit():\n"
            "                raise ValueError('invalid catalog')\n"
            "            result[name] = result.get(name, 0) + int(quantity)\n"
            "    except csv.Error:\n"
            "        raise ValueError('invalid catalog')\n"
            "    return result\n"),
        "catalog_cli.py": (
            "import json\nimport sys\n\nfrom catalog import read_catalog\n\n\n"
            "def main(argv=None):\n"
            "    argv = sys.argv[1:] if argv is None else argv\n"
            "    if len(argv) != 1:\n"
            "        print('usage: catalog_cli.py PATH', file=sys.stderr)\n"
            "        return 2\n"
            "    try:\n"
            "        with open(argv[0], encoding='utf-8', newline='') as stream:\n"
            "            data = read_catalog(stream)\n"
            "    except ValueError:\n"
            "        print('invalid catalog', file=sys.stderr)\n"
            "        return 2\n"
            "    print(json.dumps(data, sort_keys=True, ensure_ascii=False))\n"
            "    return 0\n\n\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"),
    },
}

# plausible but wrong candidates the corpus MUST reject
MUTANTS = {
    # commits each statement instead of one transaction for the whole run
    "sqlite-schema-migration": {
        "migrate.py": "import sqlite3\n\nMIGRATIONS = [\n    (2, \"ALTER TABLE accounts ADD COLUMN email TEXT\"),\n]\n\n\ndef migrate(db_path):\n    connection = sqlite3.connect(db_path, isolation_level=None)\n    try:\n        current = connection.execute(\n            \"SELECT version FROM schema_meta\").fetchone()[0]\n        pending = sorted((t, s) for t, s in MIGRATIONS if t > current)\n        applied = 0\n        for target, sql in pending:\n            connection.execute(\"BEGIN IMMEDIATE\")\n            connection.execute(sql)\n            connection.execute(\n                \"UPDATE schema_meta SET version = ?\", (target,))\n            connection.execute(\"COMMIT\")\n            applied += 1\n        return applied\n    finally:\n        connection.close()\n"
    },
    # Lexical confinement passes ordinary reads/traversal but misses symlink escapes.
    "asset-fetch-boundary": {
        "asset_fetch.py": "import os\n\n\ndef fetch_asset(base_dir, requested_path):\n    base = os.path.abspath(base_dir)\n    target = os.path.abspath(os.path.join(base, requested_path))\n    if os.path.isabs(requested_path) or not target.startswith(base + os.sep):\n        raise ValueError(\"invalid path\")\n    if not os.path.isfile(target):\n        raise ValueError(\"invalid path\")\n    with open(target, \"rb\") as stream:\n        return stream.read()\n"
    },
    "suffix-removal": {
        # correct on the public examples but mishandles the empty suffix
        "suffix.py": "def remove_suffix(text, suffix):\n    return text[:-len(suffix)] if text.endswith(suffix) else text\n",
    },
    "page-count": {
        # exact for ordinary sizes, but switches to truncation for huge integers
        "pages.py": "def page_count(total, size):\n    return -(-total // size) if total < 10**100 else total // size\n",
    },
    "record-lines": {
        # rejects arrays like the public example but accepts other non-objects
        "records.py": ("import json\n\n\ndef parse_records(text):\n"
                       "    rows = []\n"
                       "    for number, line in enumerate(text.splitlines(), 1):\n"
                       "        if not line.strip():\n"
                       "            continue\n"
                       "        try:\n"
                       "            value = json.loads(line)\n"
                       "        except ValueError:\n"
                       "            raise ValueError(f'line {number}: expected JSON object')\n"
                       "        if isinstance(value, list):\n"
                       "            raise ValueError(f'line {number}: expected JSON object')\n"
                       "        rows.append(value)\n"
                       "    return rows\n"),
    },
    "catalog-csv": {
        # CSV quoting and aggregation are right, but numeric validation is not
        "catalog.py": ("import csv\n\n\n"
                       "def read_catalog(stream):\n"
                       "    reader = csv.reader(stream)\n"
                       "    try:\n"
                       "        header = next(reader)\n"
                       "    except StopIteration:\n"
                       "        return {}\n"
                       "    if header != ['name', 'quantity']:\n"
                       "        raise ValueError('invalid catalog')\n"
                       "    result = {}\n"
                       "    for row in reader:\n"
                       "        if not row:\n"
                       "            continue\n"
                       "        if len(row) != 2:\n"
                       "            raise ValueError('invalid catalog')\n"
                       "        name, quantity = row[0].strip(), row[1].strip()\n"
                       "        if not name:\n"
                       "            raise ValueError('invalid catalog')\n"
                       "        try:\n"
                       "            value = int(quantity)\n"
                       "        except ValueError:\n"
                       "            raise ValueError('invalid catalog')\n"
                       "        result[name] = result.get(name, 0) + value\n"
                       "    return result\n"),
        "catalog_cli.py": REFERENCE_IMPL["catalog-csv"]["catalog_cli.py"],
    },
}


def _run_check(case, kind, impl):
    """Materialize a case in an owned tempdir with `impl` replacing the
    fix-file(s) and run the named corpus check. Returns the check result."""
    with tempfile.TemporaryDirectory() as root:
        root = pathlib.Path(root)
        workspace = root / "ws"
        workspace.mkdir()
        for name, content in case["initial_files"].items():
            (workspace / name).write_text(impl.get(name, content), encoding="utf-8")
        return evaluate_shadow_check(case[kind], workspace)


def evaluate_shadow_check(script, workspace):
    # late import keeps module import cheap and mirrors evaluate_paired's own wiring
    import evaluate_shadow
    return evaluate_shadow.check(script, workspace)


@pytest.mark.parametrize("case_id", sorted(REFERENCE_IMPL))
@pytest.mark.parametrize("kind", ["public_check", "heldout_assert_script"])
def test_reference_positive_control_passes_every_check(case_id, kind):
    case = next(c for c in CASES if c["id"] == case_id)
    result = _run_check(case, kind, REFERENCE_IMPL[case_id])
    assert result["passed"], f"{case_id}/{kind}: correct reference rejected:\n{result['stderr']}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_baseline_negative_control_still_proven_broken(case):
    # retain the broken-baseline contract: every initial_files variant must
    # fail public with AssertionError and heldout with any expected failure
    public = _run_check(case, "public_check", case["initial_files"])
    heldout = _run_check(case, "heldout_assert_script", case["initial_files"])
    assert public["assertion_failure"], public["stderr"]
    assert heldout["expected_failure"], heldout["stderr"]



@pytest.mark.parametrize("case_id", sorted(MUTANTS))
def test_plausible_mutant_fails_heldout_oracle(case_id):
    case = next(c for c in CASES if c["id"] == case_id)
    public = _run_check(case, "public_check", MUTANTS[case_id])
    assert public["passed"], public["stderr"]
    heldout = _run_check(case, "heldout_assert_script", MUTANTS[case_id])
    assert heldout["assertion_failure"], heldout["stderr"]


def test_migration_oracle_rejects_implicit_ddl_transaction():
    case = next(c for c in CASES if c["id"] == "sqlite-schema-migration")
    implicit = {"migrate.py": REFERENCE_IMPL[case["id"]]["migrate.py"]
                .replace("isolation_level=None", "isolation_level=''")
                .replace('connection.execute("BEGIN IMMEDIATE")', "pass")}
    public = _run_check(case, "public_check", implicit)
    assert public["passed"], public["stderr"]
    heldout = _run_check(case, "heldout_assert_script", implicit)
    assert heldout["assertion_failure"], heldout["stderr"]


def test_ledger_reference_accumulates_and_rolls_back_same_account():
    case = next(c for c in CASES if c["id"] == "ledger-batches")
    script = """
from ledger import apply_batch
state = {"balances": {"a": 10}, "seen": set()}
assert apply_batch(state, [{"id": "1", "account": "a", "delta": 2},
                           {"id": "2", "account": "a", "delta": 3}]) == {
    "balances": {"a": 15}, "seen": {"1", "2"}}
state = {"balances": {"a": 10}, "seen": set()}
try:
    apply_batch(state, [{"id": "1", "account": "a", "delta": -6},
                       {"id": "2", "account": "a", "delta": -6}])
except ValueError:
    pass
else:
    raise AssertionError("negative prefix accepted")
assert state == {"balances": {"a": 10}, "seen": set()}
"""
    result = _run_check({**case, "control": script}, "control",
                        REFERENCE_IMPL["ledger-batches"])
    assert result["passed"], result["stderr"]


def test_asset_fetch_oracle_uses_only_owned_temp_paths():
    # the historical oracle bug: probes against /etc/shadow and a
    # missing-sentinel preamble that raised ValueError outside the
    # AssertionError catch. No absolute system path may appear in the checks.
    case = next(c for c in CASES if c["id"] == "asset-fetch-boundary")
    for kind in ("public_check", "heldout_assert_script"):
        text = case[kind]
        assert "/etc/shadow" not in text and "/etc/passwd" not in text
        assert "TemporaryDirectory" in text
        assert case["initial_files"]["asset_fetch.py"] not in text



def _synthetic_arm(case_id, workspace, *, correct_impl=None, tamper_snapshot=False):
    """Build a minimal v4-style arm tree and reassess it offline."""
    import shutil
    import reassess_paired_v4
    case = next(c for c in CASES if c["id"] == case_id)
    arm_dir = workspace / "arm"
    snapshot = arm_dir / "sources"
    snapshot.mkdir(parents=True)
    impl = correct_impl or case["initial_files"]
    for name in impl:
        (snapshot / name).write_text(impl[name], encoding="utf-8")
    original_files = {k: v for k, v in case["initial_files"].items()
                      if k.endswith(".py")}
    original_hashes = {k: hashlib.sha256(impl[k].encode()).hexdigest()
                       for k in impl if k.endswith(".py")}
    if tamper_snapshot:
        (snapshot / next(k for k in original_hashes)).write_text("tampered = 1\n")
    record = {"case_id": case_id, "repeat": 1, "arm": "medium",
              "requested_effort": "medium", "final_source_hashes": original_hashes,
              "public_test_unchanged": True, "correct": False}
    (arm_dir / "result.json").write_text(json.dumps(record))
    for name in case["initial_files"]:
        if not name.endswith(".py"):
            shutil.copyfile(snapshot / next(iter(original_hashes)), arm_dir / name)
    return reassess_paired_v4.reassess_arm(case, arm_dir, workspace / "wsroot")


def test_reassessment_requires_frozen_candidate_bytes():
    # tampering the retained candidate tree must abort, not silently assess
    with tempfile.TemporaryDirectory() as root:
        with pytest.raises(ValueError, match="frozen candidate bytes changed"):
            _synthetic_arm("slugify", pathlib.Path(root), tamper_snapshot=True)


def test_reassessment_carries_provenance_and_recomputes_correctness():
    # the reassessment is an erratum, never a preregistered rerun: original
    # status and wire/native/telemetry provenance are carried verbatim while
    # correctness is recomputed from the corrected oracle only.
    with tempfile.TemporaryDirectory() as root:
        result = _synthetic_arm(
            "slugify", pathlib.Path(root),
            correct_impl=REFERENCE_IMPL["slugify"])
    assert result["correct"] is True, result["heldout"]["stderr"]
    assert result["original_correct_v4"] is False  # original label carried, not overwritten
    assert result["oracle_assessment_label"] == (
        "deterministic-oracle-assessment (not model code review)")
    assert result["frozen_source_match"] == {"slugify.py": True}
    assert result["provenance"]["source_candidate_sha256"] == \
        result["frozen_source_hashes"]
    assert result["native"] is None and result["wire"] is None  # synthetic: none claimed



def test_reassessment_recounts_all_arms_and_eligible_pairs():
    # current design: 40 attempts (10 cases x 2 arms x 2 repeats); v4's
    # historical 24/12/10 arithmetic is preserved only by recomputation on
    # the (then) 6-case plan, never by pinning it to today's corpus
    pairs = full_pairs()
    assert len(pairs) * len(ARMS) == 4 * len(CASES) == 40
    result = agg(pairs)
    assert result["pairs_total"] == 20
    assert result["pairs_complete"] == 20
    # mutate to reproduce the v4-style exclusions (one per defect class)
    coerced = good_arm(ARMS[1])
    coerced["wire"]["requests"][0]["sent_effort_raw"] = "high"
    coerced["wire"]["requests"][0]["actually_sent_effort"] = None
    pairs[0]["arms"][ARMS[1]] = coerced
    unpaired = pairs[1]
    del unpaired["arms"][ARMS[0]]
    result = agg(pairs)
    assert result["pairs_unsupported_effort"] == 1
    assert result["pairs_unpaired_or_incomplete"] == 1
    assert result["pairs_complete"] == 18
    assert result["automatic_routing"] == "NOT_QUALIFIED"


def test_all_frozen_ids_retained_with_categories():
    ids = {c["id"] for c in CASES}
    assert ids == FROZEN_IDS | ADDITIVE_IDS and len(CASES) == 10
    for case in CASES:
        assert case["categories"], f"{case['id']} lost its category metadata"



def test_paired_arms_share_input_bytes_and_order_within_pair():
    plan = trial_plan(CASES, ARMS, 2, SEED)
    assert len(plan) == 4 * len(CASES) == 40
    for case in CASES:
        rows = [t for t in plan if t["case_id"] == case["id"]]
        for repeat in (1, 2):
            arms = [t for t in rows if t["repeat"] == repeat]
            # same seed and same starting bytes for both arms of the pair
            assert len({t["seed"] for t in arms}) == 1
            assert arms[0]["arm_order"] == arms[1]["arm_order"]
        first = [t for t in rows if t["repeat"] == 1]
        second = [t for t in rows if t["repeat"] == 2]
        assert [t["arm"] for t in second] == [t["arm"] for t in first][::-1]


def test_correctness_discrimination_over_every_case_and_both_sides():
    # correctness is scored per arm; public/heldout views exist per arm too
    result = agg(full_pairs())
    for arm in ARMS:
        summary = result["per_arm"][arm]
        assert summary["correct"] == 2 * len(CASES)
        assert summary["incorrect"] == 0
        assert summary["correctness_rate"] == 1.0
        assert summary["public_passed"] == 2 * len(CASES)
        assert summary["heldout_passed"] == 2 * len(CASES)
    flipped = full_pairs()
    for p in flipped:
        for arm in ARMS:
            p["arms"][arm]["correct"] = False
    flipped_result = agg(flipped)
    for arm in ARMS:
        assert flipped_result["per_arm"][arm]["correct"] == 0
        assert flipped_result["per_arm"][arm]["incorrect"] == 2 * len(CASES)


def test_cost_estimates_reported_separately_never_as_actual_billing():
    pairs = full_pairs()
    for p in pairs:
        for arm in ARMS:
            p["arms"][arm]["native"]["cost_usd"] = 0.01
            p["arms"][arm]["native"]["cost_status"] = "estimated"
    result = agg(pairs)
    for arm in ARMS:
        summary = result["per_arm"][arm]
        # the estimate IS reported honestly, with provenance…
        assert summary["cost_usd_native_measured"] == pytest.approx(
            0.01 * 2 * len(CASES))
        assert summary["cost_usd_measured_observations"] == 2 * len(CASES)
        assert summary["cost_provenance_counts"] == {"native-reported": 2 * len(CASES)}
        assert summary["cost_status_counts"] == {"estimated": 2 * len(CASES)}
        # …but NEVER becomes actual billing and never feeds routing
        assert summary["cost_usd_actual"] is None
        assert summary["cost_usd_observations"] == 0
        assert not result["qualification_gates"]["actual_cost_observations_complete"]
        assert result["automatic_routing"] == "NOT_QUALIFIED"


def test_unobserved_cost_stays_explicit_null_not_zero():
    pairs = full_pairs()
    for p in pairs:
        for arm in ARMS:
            p["arms"][arm]["native"]["cost_usd"] = None
            p["arms"][arm]["native"]["cost_status"] = "unknown"
    result = agg(pairs)
    for arm in ARMS:
        summary = result["per_arm"][arm]
        assert summary["cost_usd_native_measured"] is None
        assert summary["cost_provenance_counts"] == {"unobserved": 2 * len(CASES)}
        assert summary["cost_usd_actual"] is None
    evidence = arm_evidence(pairs[0]["arms"][ARMS[0]])
    assert evidence["cost"]["measured"] is False
    assert evidence["cost"]["cost_usd"] is None
    assert evidence["cost"]["provenance"] == "unobserved"


def test_partial_cost_provenance_reported_verbatim():
    pairs = full_pairs()
    for p in pairs:
        for arm in ARMS:
            p["arms"][arm]["native"]["cost_status"] = "partial"
    evidence = arm_evidence(pairs[0]["arms"][ARMS[0]])
    assert evidence["cost"]["cost_status"] == "partial"
    assert evidence["cost"]["provenance"] == "native-reported"
    result = agg(pairs)
    for arm in ARMS:
        assert result["per_arm"][arm]["cost_status_counts"] == {"partial": 2 * len(CASES)}
        # partial amounts are reported, not hidden, and still not actual
        assert result["per_arm"][arm]["cost_usd_native_measured"] is not None
        assert result["per_arm"][arm]["cost_usd_actual"] is None


def test_pending_or_unavailable_classification_keeps_child_evidence_valid():
    # a pending/unavailable classifier row must not invalidate real native
    # evidence: coding outcome and cost reporting are unaffected
    for state in ("pending", "unavailable"):
        record = good_arm(ARMS[0])
        record["classifier"] = {"verdict": None, "suggested_effort": None,
                                "classification_state": state,
                                "classification_reason": "overloaded"}
        evidence = arm_evidence(record)
        assert evidence["coding_outcome_known"] is True
        assert evidence["infra_error"] is False
        assert evidence["cost"]["cost_usd"] == 0.01


def test_http_acceptance_alone_never_qualifies_routing():
    # 200 on every request with the effort field present is exactly the case
    # the study calls accepted-on-wire; backend_effective_effort stays unknown
    result = agg(full_pairs())
    assert result["qualification_gates"]["backend_accepted_on_wire"] is True
    assert result["qualification_gates"]["backend_effective_effort_known"] is False
    assert result["per_arm"][ARMS[0]]  # evidence retained
    assert result["automatic_routing"] == "NOT_QUALIFIED"
    evidence = arm_evidence(full_pairs()[0]["arms"][ARMS[0]])
    assert evidence["backend"]["support"] == "accepted-on-wire"
    assert evidence["backend"]["backend_effective_effort"] == "unknown"
