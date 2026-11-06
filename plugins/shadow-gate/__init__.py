"""Shadow gate — promotion verdicts for shadow-mode decisions, bound to evidence.

A *shadow* decision records what a classifier or ranker would have done and never
applies it. This plugin is the visible half of that contract: it reads the
append-only ledgers that producer plugins write themselves (via the ledger
module's ``shared_ledger(root).record_verdict`` / ``record_outcome``) and
reports, per decision, whether the operator's explicitly configured promotion
policy is satisfied.

House posture honoured here:

* Nothing self-activates. No hooks, no behaviour change, and no decision is ever
  qualified by default: a decision without a policy is ``NOT_QUALIFIED`` with
  reason ``no_policy`` — the same constant-by-design stance as ``automatic_routing``
  in ``evaluation/evaluate_paired.py``. Promotion needs a policy the operator wrote.
* Explicit beats implicit: thresholds come only from
  ``plugins.entries.shadow-gate.settings.decisions``, parsed into frozen policies
  once, at register. A bad entry is a ``ValueError`` naming the decision, not a
  silently ignored key.
* Fail-open is visible: every report carries pair counts, agreement,
  false-positive rate, the unpaired remainder and the failed gate names — never a
  bare "ok".
* Native stores stay authoritative: this plugin's own ledger files are the only
  thing it ever touches, and reporting only reads them.

The ledger itself lives in ``ledger.py`` and is loaded by file path, reusing the
process-wide instance in ``sys.modules["hermes_plugins_shadow_gate"]`` so every
producer plugin shares one ledger.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import TYPE_CHECKING, Final, Protocol, cast

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

# Stdlib logging, not structlog: structlog is not an installed dependency of the
# Hermes host and this plugin must import with stdlib-only dependencies, so the
# stdlib logger is the only available bridge (same as every sibling plugin).
logger = logging.getLogger(__name__)

_MODULE_NAME: Final = "hermes_plugins_shadow_gate"
_DEFAULT_MAX_ROWS: Final = 5000
_POLICY_KEYS: Final = (
    "min_pairs",
    "min_agreement",
    "max_false_positive",
    "positive_verdicts",
    "positive_outcomes",
)
_STATUS_OK: Final = 0
_STATUS_ERROR: Final = 2

_TOOL_SCHEMA: Final[dict[str, object]] = {
    "name": "shadow_status",
    "description": (
        "Report the shadow-decision promotion evidence this plugin's ledgers hold: "
        "per decision the pair count, agreement, false-positive rate, unpaired rows, "
        "and the qualification status with every failed gate named. Reads state only; "
        "records nothing and changes nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "description": "Report one decision only (default: every configured or recorded one).",
            },
        },
        "required": [],
    },
}


class ReportView(Protocol):
    """The promotion report fields this plugin renders (ledger.PromotionReport)."""

    @property
    def pairs(self) -> int: ...
    @property
    def agreement(self) -> float: ...
    @property
    def false_positive_rate(self) -> float: ...
    @property
    def unpaired_verdicts(self) -> int: ...
    @property
    def unpaired_outcomes(self) -> int: ...
    @property
    def status(self) -> str: ...
    @property
    def reasons(self) -> tuple[str, ...]: ...
    def to_dict(self) -> dict[str, object]: ...


class LedgerViewHandle(Protocol):
    """One decision's joined evidence plus its unpaired remainder."""

    @property
    def pairs(self) -> tuple[object, ...]: ...
    @property
    def unpaired_verdicts(self) -> tuple[object, ...]: ...
    @property
    def unpaired_outcomes(self) -> tuple[object, ...]: ...


class LedgerHandle(Protocol):
    """The read side of a ledger instance."""

    def snapshot(self, decision: str) -> LedgerViewHandle: ...
    def decisions(self) -> tuple[str, ...]: ...


class LedgerApi(Protocol):
    """The ledger-module entry points this plugin calls (see ledger.py)."""

    def validate_decision(self, name: str) -> str: ...
    def shared_ledger(
        self,
        root: Path,
        *,
        now: Callable[[], datetime] | None = None,
        max_rows_per_decision: int = _DEFAULT_MAX_ROWS,
    ) -> LedgerHandle: ...
    def promotion_policy(
        self,
        *,
        min_pairs: int,
        min_agreement: float,
        max_false_positive: float,
        positive_verdicts: Iterable[str],
        positive_outcomes: Iterable[str],
    ) -> object: ...
    def evaluate(
        self,
        pairs: Sequence[object],
        policy: object | None,
        *,
        unpaired_verdicts: int = 0,
        unpaired_outcomes: int = 0,
    ) -> ReportView: ...


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated plugin configuration. ``policies`` values are the ledger module's
    frozen ``PromotionPolicy`` records, keyed by validated decision name."""

    ledger_dir: Path
    max_rows_per_decision: int
    policies: Mapping[str, object]


def _load_ledger_module() -> ModuleType:
    """Load ``ledger.py`` once per process: ``sys.modules`` first, else by path.

    Checking the shared name first is what keeps one ledger (one row-count cache,
    one append lock) across every plugin that path-loads it.
    """
    shared = sys.modules.get(_MODULE_NAME)
    if shared is not None:
        return shared
    path = Path(__file__).resolve().parent / "ledger.py"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"shadow-gate: cannot load its own ledger.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # A half-initialized module must not stay registered: the next loader
        # would receive an object missing the functions it is about to call.
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


def _require_int(raw: object, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(
            f"shadow-gate: {label} must be an integer (got {type(raw).__name__})"
        )
    return raw


def _require_float(raw: object, label: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"shadow-gate: {label} must be a number (got {type(raw).__name__})")
    return float(raw)


def _require_str_tuple(raw: object, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise TypeError(
            f"shadow-gate: {label} must be a list of strings (got {type(raw).__name__})"
        )
    if not raw:
        raise ValueError(f"shadow-gate: {label} must not be empty")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise TypeError(
                f"shadow-gate: {label} must contain strings (got {type(item).__name__})"
            )
        if not item.strip():
            raise ValueError(f"shadow-gate: {label} must not contain blank strings")
        values.append(item.strip())
    return tuple(values)


def _parse_policy(name: str, entry: object, api: LedgerApi) -> object:
    """Parse one configured policy into a frozen ledger policy, or say what is wrong."""
    if not isinstance(entry, Mapping):
        raise TypeError(
            f"shadow-gate: decisions.{name} must be a mapping with "
            f"{', '.join(_POLICY_KEYS)} (got {type(entry).__name__})"
        )
    missing = [key for key in _POLICY_KEYS if key not in entry]
    if missing:
        raise ValueError(f"shadow-gate: decisions.{name} is missing {', '.join(missing)}")
    min_agreement = _require_float(entry["min_agreement"], f"decisions.{name}.min_agreement")
    max_false_positive = _require_float(
        entry["max_false_positive"], f"decisions.{name}.max_false_positive"
    )
    try:
        return api.promotion_policy(
            min_pairs=_require_int(entry["min_pairs"], f"decisions.{name}.min_pairs"),
            min_agreement=min_agreement,
            max_false_positive=max_false_positive,
            positive_verdicts=_require_str_tuple(
                entry["positive_verdicts"], f"decisions.{name}.positive_verdicts"
            ),
            positive_outcomes=_require_str_tuple(
                entry["positive_outcomes"], f"decisions.{name}.positive_outcomes"
            ),
        )
    except ValueError as exc:
        raise ValueError(f"shadow-gate: decisions.{name}: {exc}") from exc


def _parse_policies(raw: object, api: LedgerApi) -> Mapping[str, object]:
    """Parse the ``decisions`` setting once, at the boundary; order is preserved."""
    if raw is None:
        return MappingProxyType({})
    if not isinstance(raw, Mapping):
        raise TypeError(
            f"shadow-gate: decisions must be a mapping of decision name to policy "
            f"(got {type(raw).__name__})"
        )
    policies: dict[str, object] = {}
    for name, entry in raw.items():
        if not isinstance(name, str):
            raise TypeError(
                f"shadow-gate: decision names must be strings (got {type(name).__name__})"
            )
        try:
            validated = api.validate_decision(name)
        except ValueError as exc:
            raise ValueError(f"shadow-gate: {exc}") from exc
        policies[validated] = _parse_policy(validated, entry, api)
    return MappingProxyType(policies)


def _ledger_dir(raw: object) -> Path:
    if not isinstance(raw, str):
        raise TypeError(
            f"shadow-gate: ledger_dir must be a string (got {type(raw).__name__})"
        )
    if raw.strip():
        return Path(raw.strip()).expanduser()
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "plugin-data" / "shadow-gate"


def _build_settings(
    get_config: Callable[[str, object], object], api: LedgerApi
) -> Settings:
    """Read every declared setting once and freeze it into typed values."""
    max_rows = _require_int(
        get_config("max_rows_per_decision", _DEFAULT_MAX_ROWS), "max_rows_per_decision"
    )
    if max_rows < 1:
        raise ValueError(
            f"shadow-gate: max_rows_per_decision must be >= 1 (got {max_rows!r})"
        )
    return Settings(
        ledger_dir=_ledger_dir(get_config("ledger_dir", "")),
        max_rows_per_decision=max_rows,
        policies=_parse_policies(get_config("decisions", {}), api),
    )


def _requested_decision(raw: object) -> str | None:
    """Parse the optional ``decision`` argument; absent/empty means every decision."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TypeError(
            f"shadow-gate: decision must be a string (got {type(raw).__name__})"
        )
    return raw.strip() or None


def _reports(
    api: LedgerApi,
    ledger: LedgerHandle,
    settings: Settings,
    decision: str | None,
) -> dict[str, ReportView]:
    """Evaluate every reportable decision: configured ones plus those with rows."""
    if decision is not None:
        names = [str(api.validate_decision(decision))]
    else:
        names = sorted({*settings.policies, *ledger.decisions()})
    reports: dict[str, ReportView] = {}
    for name in names:
        view = ledger.snapshot(name)
        reports[name] = api.evaluate(
            view.pairs,
            settings.policies.get(name),
            unpaired_verdicts=len(view.unpaired_verdicts),
            unpaired_outcomes=len(view.unpaired_outcomes),
        )
    return reports


def _fmt_rate(value: float) -> str:
    return f"{value:.3f}"


def _format_table(reports: Mapping[str, ReportView]) -> str:
    """Fixed-column text report; reasons are named in full, never summarised away."""
    if not reports:
        return "shadow-status: no decisions configured and no ledger rows recorded"
    header = ("decision", "pairs", "agreement", "fpr", "unpaired v/o", "status", "reasons")
    rows = [
        (
            name,
            str(report.pairs),
            _fmt_rate(report.agreement),
            _fmt_rate(report.false_positive_rate),
            f"{report.unpaired_verdicts}/{report.unpaired_outcomes}",
            report.status,
            ",".join(report.reasons),
        )
        for name, report in reports.items()
    ]
    widths = [
        max(len(header[column]), *(len(row[column]) for row in rows))
        for column in range(len(header))
    ]
    lines = [
        "  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)).rstrip()
        for row in (*[header], *rows)
    ]
    return "\n".join(lines)


def register(ctx: PluginContext) -> None:
    """Register the read-only status tool and CLI command; no hooks, nothing applied."""
    api = cast(LedgerApi, _load_ledger_module())
    settings = _build_settings(ctx.get_config, api)
    ledger = api.shared_ledger(
        settings.ledger_dir, max_rows_per_decision=settings.max_rows_per_decision
    )

    def shadow_status(args: dict[str, object], **_kwargs: object) -> str:
        payload = args or {}
        reports = _reports(api, ledger, settings, _requested_decision(payload.get("decision")))
        return json.dumps(
            {"decisions": {name: report.to_dict() for name, report in reports.items()}},
            indent=2,
        )

    ctx.register_tool(
        name="shadow_status",
        toolset="shadow-gate",
        schema=_TOOL_SCHEMA,
        handler=shadow_status,
        description="Report shadow-decision promotion evidence (pairs, agreement, "
        "false-positive rate, unpaired rows, status with failed gates). Read-only.",
    )

    def cli_setup(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--decision",
            default="",
            help="report one decision only (default: every configured or recorded one)",
        )
        sub.add_argument(
            "--json", action="store_true", help="emit the machine-readable report instead of a table"
        )

    def cli_handler(args: argparse.Namespace) -> int:
        # A NOT_QUALIFIED verdict is a normal answer, so it exits 0; only an
        # unusable ledger or an invalid --decision is an error (exit 2).
        try:
            reports = _reports(api, ledger, settings, _requested_decision(args.decision))
        except (OSError, ValueError) as exc:
            print(f"shadow-status: {exc}", file=sys.stderr)
            return _STATUS_ERROR
        if args.json:
            print(
                json.dumps(
                    {
                        "decisions": {
                            name: report.to_dict() for name, report in reports.items()
                        }
                    },
                    indent=2,
                )
            )
        else:
            print(_format_table(reports))
        return _STATUS_OK

    ctx.register_cli_command(
        name="shadow-status",
        help="Report shadow-decision promotion evidence per decision (table or --json).",
        setup_fn=cli_setup,
        handler_fn=cli_handler,
        description=(
            "Reads this plugin's shadow ledgers and reports whether each decision's\n"
            "explicitly configured promotion policy is satisfied. Qualifies nothing\n"
            "by itself: a decision without a policy stays NOT_QUALIFIED (no_policy)."
        ),
    )
    logger.info(
        "shadow-gate: reporting %d configured decision(s), ledgers under %s",
        len(settings.policies),
        settings.ledger_dir,
    )
