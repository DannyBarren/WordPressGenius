from __future__ import annotations

import pytest

from evals.harness import DESTRUCTIVE_CATEGORIES, destructive_cases, load_cases, run_case, run_suite


CASES = load_cases()


def test_eval_suite_covers_required_destructive_categories() -> None:
    destructive = destructive_cases(CASES)
    categories = {case.category for case in destructive}

    assert len(destructive) >= 30
    for required in ("publish", "delete", "refund"):
        assert required in categories
    assert categories <= DESTRUCTIVE_CATEGORIES


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_golden_prompt_approval_gate(case) -> None:
    result = run_case(case)
    assert result.passed, result.detail or f"{case.id} failed"


def test_destructive_cases_all_expect_confirmation() -> None:
    for case in destructive_cases(CASES):
        assert case.expect_confirmation is True


def test_harness_run_suite_is_green() -> None:
    results = run_suite(CASES)
    failed = [result for result in results if not result.passed]
    assert not failed, ", ".join(f"{item.case.id}: {item.detail}" for item in failed)
