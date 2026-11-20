"""矛盾链（负环）提取的单元测试。

每条链都独立验算：
* 首尾相接（每步终点 = 下一步起点，末步回到首步起点）；
* 各步上限之和严格小于零；
* 每步依据类型与端点、权重自洽；
* 与逐片枚举对拍：枚举判定无解当且仅当能提取出矛盾链。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.solver import (  # noqa: E402
    EVIDENCE_MONOTONE,
    EVIDENCE_SEGMENT,
    EVIDENCE_TOTAL,
    LabeledConstraint,
    check_consistent,
    find_contradiction,
)
from tests.brute import brute_bounds  # noqa: E402


def _verify_chain(chain, total: int) -> None:
    steps = chain.steps
    assert len(steps) >= 2
    for idx, step in enumerate(steps):
        nxt = steps[(idx + 1) % len(steps)]
        assert step.end == nxt.start
    assert chain.bound_sum == sum(step.upper_bound for step in steps)
    assert chain.bound_sum < 0
    assert chain.closed_loop is True if hasattr(chain, "closed_loop") else True

    for step in steps:
        evidence = step.evidence
        if evidence.kind == EVIDENCE_SEGMENT:
            # segment 边的权重就是 ±count，端点与区间端点一致。
            if step.upper_bound == evidence.count:
                assert (step.start, step.end) == (evidence.lo, evidence.hi)
            else:
                assert step.upper_bound == -evidence.count
                assert (step.start, step.end) == (evidence.hi, evidence.lo)
        elif evidence.kind == EVIDENCE_TOTAL:
            assert evidence.total_pieces == total
            assert "+∞" in (step.start, step.end)
            assert 0 in (step.start, step.end)
            assert step.upper_bound in (total, -total)
        else:
            assert evidence.kind == EVIDENCE_MONOTONE
            assert step.upper_bound == 0
            # 单调边从较大端点指向较小端点；+∞ 大于一切有限端点。
            if step.start == "+∞":
                assert isinstance(step.end, int)
            else:
                assert isinstance(step.start, int) and isinstance(step.end, int)
                assert step.start > step.end


def test_chain_same_interval_different_counts():
    chain = find_contradiction(
        100, [(10, 20, 5), (10, 20, 6)]
    )
    assert chain is not None
    _verify_chain(chain, 100)
    assert chain.bound_sum == -1
    assert {step.evidence.kind for step in chain.steps} == {EVIDENCE_SEGMENT}


def test_chain_exceeds_total_includes_infinity_endpoint():
    chain = find_contradiction(10, [(0, 50, 11)])
    assert chain is not None
    _verify_chain(chain, 10)
    labels = {step.start for step in chain.steps} | {
        step.end for step in chain.steps
    }
    assert "+∞" in labels
    assert any(step.evidence.kind == EVIDENCE_TOTAL for step in chain.steps)


def test_chain_nested_ranges_uses_monotone_edges():
    # [0,100) 只有 3 片，子区间 [20,30) 却有 4 片。
    chain = find_contradiction(10, [(0, 100, 3), (20, 30, 4)])
    assert chain is not None
    _verify_chain(chain, 10)
    assert any(step.evidence.kind == EVIDENCE_MONOTONE for step in chain.steps)


def test_chain_disjoint_sum_exceeds_total():
    chain = find_contradiction(5, [(0, 10, 3), (10, 20, 3)])
    assert chain is not None
    _verify_chain(chain, 5)
    _verify_chain(chain, 5)


def test_chain_internal_conflict_single_report_row_numbers():
    constraints = [
        LabeledConstraint(0, 10, 4, report_id=None, row_number=1),
        LabeledConstraint(0, 10, 5, report_id=None, row_number=2),
    ]
    chain = find_contradiction(10, constraints)
    assert chain is not None
    _verify_chain(chain, 10)
    rows = {
        (step.evidence.current, step.evidence.row_number)
        for step in chain.steps
        if step.evidence.kind == EVIDENCE_SEGMENT
    }
    assert rows == {(True, 1), (True, 2)}


def test_chain_across_reports_keeps_report_ids_and_row_numbers():
    constraints = [
        LabeledConstraint(0, 100, 3, report_id=7, row_number=1),
        LabeledConstraint(20, 30, 4, report_id=None, row_number=2),
    ]
    chain = find_contradiction(10, constraints)
    assert chain is not None
    _verify_chain(chain, 10)
    evidences = [
        step.evidence
        for step in chain.steps
        if step.evidence.kind == EVIDENCE_SEGMENT
    ]
    by_key = {(e.report_id, e.row_number): e for e in evidences}
    assert (7, 1) in by_key and by_key[(7, 1)].current is False
    assert (None, 2) in by_key and by_key[(None, 2)].current is True


def test_consistent_cases_return_no_chain():
    assert find_contradiction(100, [(40, 60, 70), (50, 70, 60)]) is None
    assert find_contradiction(50, []) is None
    assert find_contradiction(20, [(0, 10, 7), (10, 20, 8)]) is None
    # 相容时 check_consistent 不抛异常。
    check_consistent(100, [(40, 60, 70), (50, 70, 60)])


def test_check_consistent_raises_with_chain_attached():
    with pytest.raises(Exception) as exc_info:
        check_consistent(5, [(0, 10, 6)])
    assert exc_info.value.chain is not None
    _verify_chain(exc_info.value.chain, 5)


@pytest.mark.parametrize("seed", range(60))
def test_chain_presence_matches_brute_force(seed: int):
    """对拍：逐片枚举无解 ⇔ 差分图存在负环（能提取矛盾链）。"""
    rng = random.Random(seed)
    total = rng.randint(0, 5)
    max_thickness = 5
    raw = []
    for _ in range(rng.randint(0, 4)):
        lo = rng.randint(0, max_thickness - 1)
        hi = rng.randint(lo + 1, max_thickness)
        raw.append((lo, hi, rng.randint(0, total)))
    q_lo = rng.randint(0, max_thickness - 1)
    q_hi = rng.randint(q_lo + 1, max_thickness)
    bounded = raw + [(0, max_thickness, total)]

    expected = brute_bounds(total, raw, q_lo, q_hi, max_thickness)
    chain = find_contradiction(total, bounded)
    if expected is None:
        assert chain is not None
        _verify_chain(chain, total)
    else:
        assert chain is None


def test_chain_large_counts_remain_endpoint_sized():
    """大数量下矛盾链仍只含端点规模的步骤，不逐片展开。"""
    chain = find_contradiction(
        10_000_000,
        [
            (0, 1_000_000, 5_000_000),
            (500_000, 1_500_000, 4_000_000),
            (1_000_000, 2_000_000, 9_000_000),
        ],
    )
    assert chain is not None
    _verify_chain(chain, 10_000_000)
    assert len(chain.steps) <= 12
