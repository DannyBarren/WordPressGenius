"""Offline golden-prompt eval harness.

The model is scripted. SafetyLayer is the system under test: a mocked planner
that labels publish, delete, refund, and other writes as low-risk must still
require explicit approval before anything can execute.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agents.crew import _llm_plan_actions, _plan_actions
from core.safety import SafetyLayer

CASES_PATH = Path(__file__).resolve().parent / "cases.yaml"
DESTRUCTIVE_CATEGORIES = frozenset(
    {
        "publish",
        "delete",
        "refund",
        "woocommerce_write",
        "settings",
        "theme",
        "plugin",
        "bulk",
        "seo",
        "undo",
        "plugin_action",
        "update",
    }
)


class ScriptedRouter:
    """LLMRouter stand-in that returns a fixture plan and never calls a network."""

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.payload = payload
        self.enabled = True
        self.calls: list[tuple[str, str, str]] = []

    def complete_json(self, role, system_prompt, user_prompt, *, max_output_tokens=None):
        self.calls.append((role, system_prompt, user_prompt))
        return self.payload


@dataclass(frozen=True)
class EvalCase:
    id: str
    prompt: str
    category: str
    mocked_plan: dict[str, Any]
    expect_confirmation: bool = True
    must_include_operations: tuple[str, ...] = ()
    also_check_rules: bool = False


@dataclass
class CaseResult:
    case: EvalCase
    passed: bool
    requires_confirmation: bool | None = None
    operations: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    detail: str = ""


def load_cases(path: Path | None = None) -> list[EvalCase]:
    raw = yaml.safe_load((path or CASES_PATH).read_text(encoding="utf-8")) or {}
    items = raw.get("cases", raw if isinstance(raw, list) else [])
    cases: list[EvalCase] = []
    for item in items:
        cases.append(
            EvalCase(
                id=str(item["id"]),
                prompt=str(item["prompt"]),
                category=str(item["category"]),
                mocked_plan=dict(item.get("mocked_plan") or {}),
                expect_confirmation=bool(item.get("expect_confirmation", True)),
                must_include_operations=tuple(item.get("must_include_operations") or []),
                also_check_rules=bool(item.get("also_check_rules", False)),
            )
        )
    return cases


def run_case(case: EvalCase, *, app_role: str = "admin") -> CaseResult:
    router = ScriptedRouter(case.mocked_plan)
    planned = _llm_plan_actions(case.prompt, router, {})
    if planned is None:
        return CaseResult(case=case, passed=False, detail="mocked planner returned no actions")
    actions, _reasoning = planned
    decision = SafetyLayer().evaluate(actions, app_role=app_role)
    operations = [action.operation.value for action in actions]
    reasons: list[str] = []
    if decision.requires_confirmation is not case.expect_confirmation:
        reasons.append(
            f"confirmation={decision.requires_confirmation} expected {case.expect_confirmation}"
        )
    missing = [op for op in case.must_include_operations if op not in operations]
    if missing:
        reasons.append(f"missing operations {missing}; planned {operations}")

    if case.also_check_rules:
        rule_actions = _plan_actions(case.prompt)
        rule_decision = SafetyLayer().evaluate(rule_actions, app_role=app_role)
        if rule_decision.requires_confirmation is not case.expect_confirmation:
            reasons.append(
                "rule planner confirmation="
                f"{rule_decision.requires_confirmation} expected {case.expect_confirmation}"
            )

    return CaseResult(
        case=case,
        passed=not reasons,
        requires_confirmation=decision.requires_confirmation,
        operations=operations,
        reasons=decision.reasons,
        detail="; ".join(reasons),
    )


def run_suite(cases: list[EvalCase] | None = None) -> list[CaseResult]:
    return [run_case(case) for case in (cases or load_cases())]


def render_report(results: list[CaseResult]) -> str:
    passed = sum(1 for result in results if result.passed)
    lines = [f"{passed}/{len(results)} golden-prompt evals passed", ""]
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        ops = ",".join(result.operations) or "-"
        line = f"  {mark:4}  {result.case.id:36}  confirm={result.requires_confirmation}  ops={ops}"
        if result.detail:
            line += f"  ({result.detail})"
        lines.append(line)
    return "\n".join(lines)


def destructive_cases(cases: list[EvalCase] | None = None) -> list[EvalCase]:
    return [case for case in (cases or load_cases()) if case.category in DESTRUCTIVE_CATEGORIES]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline golden-prompt approval evals.")
    parser.add_argument("--cases", type=Path, default=CASES_PATH, help="YAML case file")
    args = parser.parse_args(argv)
    results = run_suite(load_cases(args.cases))
    print(render_report(results))
    return 0 if all(result.passed for result in results) else 1
