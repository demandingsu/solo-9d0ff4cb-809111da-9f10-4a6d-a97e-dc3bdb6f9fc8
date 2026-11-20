"""API 集成测试：登记、补报、冲突拒收、查询、原结果仍可查。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def make_inspection(client, total_pieces, note=None):
    response = client.post(
        "/inspections", json={"total_pieces": total_pieces, "note": note}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _assert_chain_well_formed(detail: dict) -> dict:
    """校验 409 回执中的矛盾链结构与可验算性质，返回链本身。"""
    chain = detail["contradiction_chain"]
    steps = chain["steps"]
    assert len(steps) >= 2
    # 首尾相接：每步终点等于下一步起点，末步终点回到首步起点。
    for idx, step in enumerate(steps):
        nxt = steps[(idx + 1) % len(steps)]
        assert step["end"] == nxt["start"]
    # 上限之和严格小于零。
    assert chain["bound_sum"] == sum(step["upper_bound"] for step in steps)
    assert chain["bound_sum"] < 0
    assert chain["closed_loop"] is True

    # 回查行与链中引用的 segment 依据完全对应（去重后一致）。
    referenced = set()
    for step in steps:
        basis = step["basis"]
        assert basis["kind"] in {"segment", "total", "monotone"}
        if basis["kind"] == "segment":
            referenced.add(
                (basis["source"], basis["report_id"], basis["row_number"])
            )
        elif basis["kind"] == "total":
            assert isinstance(basis["total_pieces"], int)
        else:
            assert "单调" in basis["rule"]
    recheck = {
        (row["source"], row["report_id"], row["row_number"])
        for row in detail["recheck_rows"]
    }
    assert recheck == referenced
    return chain


def test_create_and_get_inspection(client):
    created = make_inspection(client, 100, "批次 A")
    assert created["id"] == 1
    assert created["total_pieces"] == 100
    assert created["note"] == "批次 A"
    assert created["report_count"] == 0
    assert created["segment_count"] == 0

    detail = client.get("/inspections/1")
    assert detail.status_code == 200
    assert detail.json()["reports"] == []

    listed = client.get("/inspections").json()
    assert [item["id"] for item in listed] == [1]

    assert client.get("/inspections/999").status_code == 404


def test_validation_errors(client):
    assert client.post("/inspections", json={"total_pieces": -1}).status_code == 422
    make_inspection(client, 10)

    # hi 必须大于 lo
    response = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 10, "hi": 10, "count": 1}]},
    )
    assert response.status_code == 422

    # 空补报不允许
    assert (
        client.post("/inspections/1/reports", json={"segments": []}).status_code
        == 422
    )

    # 负计数不允许
    response = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 0, "hi": 10, "count": -1}]},
    )
    assert response.status_code == 422


def test_bounds_overlapping_reports(client):
    make_inspection(client, 100)
    first = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 40, "hi": 60, "count": 70}]},
    )
    assert first.status_code == 201
    second = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 50, "hi": 70, "count": 60}]},
    )
    assert second.status_code == 201
    assert second.json()["inspection_id"] == 1
    assert len(second.json()["segments"]) == 1

    bounds = client.get(
        "/inspections/1/bounds", params={"qualified_lo": 50, "qualified_hi": 60}
    )
    assert bounds.status_code == 200
    assert bounds.json() == {
        "inspection_id": 1,
        "total_pieces": 100,
        "qualified_lo": 50,
        "qualified_hi": 60,
        "min_count": 30,
        "max_count": 60,
    }

    # 详情能看到两次补报的全部区间
    detail = client.get("/inspections/1").json()
    assert detail["report_count"] == 2
    assert detail["segment_count"] == 2
    assert [
        (s["lo"], s["hi"], s["count"]) for r in detail["reports"] for s in r["segments"]
    ] == [(40, 60, 70), (50, 70, 60)]


def test_conflicting_report_is_rejected_atomically(client):
    make_inspection(client, 100)
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 30, "hi": 50, "count": 40}]},
        ).status_code
        == 201
    )

    before = client.get("/inspections/1").json()

    # 同一区间报出不同计数 => 与已有计数矛盾，整份拒收
    conflict = client.post(
        "/inspections/1/reports",
        json={
            "segments": [
                {"lo": 0, "hi": 10, "count": 5},  # 这一条本身没问题
                {"lo": 30, "hi": 50, "count": 41},  # 与已有计数冲突
            ]
        },
    )
    assert conflict.status_code == 409
    body = conflict.json()
    assert "拒收" in body["detail"]["message"]
    _assert_chain_well_formed(body["detail"])

    after = client.get("/inspections/1").json()
    assert after == before  # 原结果完全不变，且半份报告没有被写入
    assert after["report_count"] == 1
    assert len(after["reports"][0]["segments"]) == 1

    # 拒收后仍可正常查询原结果
    bounds = client.get(
        "/inspections/1/bounds", params={"qualified_lo": 30, "qualified_hi": 50}
    )
    assert bounds.status_code == 200
    assert bounds.json()["min_count"] == 40
    assert bounds.json()["max_count"] == 40


def test_conflict_inside_single_report_rejects_whole_report(client):
    make_inspection(client, 10)
    response = client.post(
        "/inspections/1/reports",
        json={
            "segments": [
                {"lo": 0, "hi": 10, "count": 4},
                {"lo": 0, "hi": 10, "count": 5},
            ]
        },
    )
    assert response.status_code == 409
    detail = client.get("/inspections/1").json()
    assert detail["report_count"] == 0
    assert detail["segment_count"] == 0


def test_report_exceeding_total_is_rejected(client):
    make_inspection(client, 5)
    response = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 0, "hi": 10, "count": 6}]},
    )
    assert response.status_code == 409
    assert client.get("/inspections/1").json()["report_count"] == 0


def test_missing_inspection_for_reports(client):
    payload = {"segments": [{"lo": 0, "hi": 10, "count": 1}]}
    assert client.post("/inspections/42/reports", json=payload).status_code == 404
    assert client.get("/inspections/42/reports").status_code == 404
    assert (
        client.get(
            "/inspections/42/bounds", params={"qualified_lo": 0, "qualified_hi": 10}
        ).status_code
        == 404
    )


def test_bounds_validation(client):
    make_inspection(client, 10)
    bad = client.get(
        "/inspections/1/bounds", params={"qualified_lo": 10, "qualified_hi": 10}
    )
    assert bad.status_code == 422
    negative = client.get(
        "/inspections/1/bounds", params={"qualified_lo": -1, "qualified_hi": 10}
    )
    assert negative.status_code == 422


def test_patch_total_conflicting_is_rejected(client):
    make_inspection(client, 100)
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 0, "hi": 50, "count": 60}]},
        ).status_code
        == 201
    )

    conflict = client.patch("/inspections/1", json={"total_pieces": 50})
    assert conflict.status_code == 409
    assert client.get("/inspections/1").json()["total_pieces"] == 100

    ok = client.patch("/inspections/1", json={"note": "已复核"})
    assert ok.status_code == 200
    assert ok.json()["note"] == "已复核"
    assert ok.json()["total_pieces"] == 100


def test_patch_note_to_null(client):
    make_inspection(client, 10, "旧备注")
    response = client.patch("/inspections/1", json={"note": None})
    assert response.status_code == 200
    assert response.json()["note"] is None


def test_docs_available(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_409_chain_same_interval_different_counts(client):
    """同区间不同计数：矛盾链只引用两个 segment 依据，上限和为负。"""
    make_inspection(client, 100)
    client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 30, "hi": 50, "count": 40}]},
    )
    conflict = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 30, "hi": 50, "count": 41}]},
    )
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    chain = _assert_chain_well_formed(detail)

    # 回查行：旧报告 1 的第 1 行 + 本次表第 1 行。
    assert detail["recheck_rows"] == [
        {
            "source": "本次补报",
            "report_id": None,
            "row_number": 1,
            "lo": 30,
            "hi": 50,
            "count": 41,
        },
        {
            "source": "已接收报告",
            "report_id": 1,
            "row_number": 1,
            "lo": 30,
            "hi": 50,
            "count": 40,
        },
    ]
    kinds = [step["basis"]["kind"] for step in chain["steps"]]
    assert kinds.count("segment") == 2
    # 两个方向相反的等式：F(50)-F(30)<=40 与 F(30)-F(50)<=-41，和为 -1。
    assert chain["bound_sum"] == -1


def test_409_chain_inside_single_report_points_at_current_rows(client):
    """单份报告内部冲突：回查行全部来自本次表，report_id 为 null。"""
    make_inspection(client, 10)
    response = client.post(
        "/inspections/1/reports",
        json={
            "segments": [
                {"lo": 0, "hi": 10, "count": 4},
                {"lo": 0, "hi": 10, "count": 5},
            ]
        },
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    chain = _assert_chain_well_formed(detail)
    assert {row["source"] for row in detail["recheck_rows"]} == {"本次补报"}
    assert {row["row_number"] for row in detail["recheck_rows"]} == {1, 2}
    assert all(row["report_id"] is None for row in detail["recheck_rows"])
    assert chain["bound_sum"] == -1


def test_409_chain_exceeding_total_uses_total_evidence(client):
    """计数超过总片数：链中必须出现登记总片数依据（含数值）与 +∞ 端点。"""
    make_inspection(client, 5)
    response = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 0, "hi": 10, "count": 6}]},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    chain = _assert_chain_well_formed(detail)
    total_steps = [
        step
        for step in chain["steps"]
        if step["basis"]["kind"] == "total"
    ]
    assert total_steps, "矛盾链必须引用登记总片数依据"
    assert all(step["basis"]["total_pieces"] == 5 for step in total_steps)
    endpoints = {step["start"] for step in chain["steps"]} | {
        step["end"] for step in chain["steps"]
    }
    assert "+∞" in endpoints
    # 回查行只有本次表第 1 行。
    assert detail["recheck_rows"] == [
        {
            "source": "本次补报",
            "report_id": None,
            "row_number": 1,
            "lo": 0,
            "hi": 10,
            "count": 6,
        }
    ]


def test_409_chain_nested_ranges_across_reports(client):
    """跨报告的嵌套区间冲突：链同时引用旧报告行号与本次表行号。"""
    make_inspection(client, 10)
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 0, "hi": 100, "count": 3}]},
        ).status_code
        == 201
    )
    # 第二份报告含两行：第 1 行相容，第 2 行与旧报告冲突。
    response = client.post(
        "/inspections/1/reports",
        json={
            "segments": [
                {"lo": 0, "hi": 5, "count": 1},
                {"lo": 20, "hi": 30, "count": 4},
            ]
        },
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    _assert_chain_well_formed(detail)
    # 回查行必须点到具体行：旧报告 1 第 1 行、本次表第 2 行都要在清单中。
    # （同一组约束可能存在多条负环，Bellman-Ford 返回其中一条，
    # 因此相容的第 1 行是否被顺带引用不作硬性要求。）
    keys = {
        (row["source"], row["report_id"], row["row_number"])
        for row in detail["recheck_rows"]
    }
    assert ("已接收报告", 1, 1) in keys
    assert ("本次补报", None, 2) in keys


def test_409_chain_patch_total_conflict(client):
    """PATCH 更正总片数冲突时，回执同样带矛盾链与总片数依据。"""
    make_inspection(client, 100)
    client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 0, "hi": 50, "count": 60}]},
    )
    conflict = client.patch("/inspections/1", json={"total_pieces": 50})
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    chain = _assert_chain_well_formed(detail)
    assert any(
        step["basis"]["kind"] == "total"
        and step["basis"]["total_pieces"] == 50
        for step in chain["steps"]
    )
    # 原登记不变。
    assert client.get("/inspections/1").json()["total_pieces"] == 100


def test_409_rejection_does_not_block_other_batches(client):
    """一个批次的冲突拒收不影响其他批次继续提交补报。"""
    first = make_inspection(client, 10)
    second = make_inspection(client, 20)
    assert first["id"] == 1 and second["id"] == 2

    bad = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 0, "hi": 10, "count": 11}]},
    )
    assert bad.status_code == 409
    _assert_chain_well_formed(bad.json()["detail"])

    # 另一批次照常接收。
    ok = client.post(
        "/inspections/2/reports",
        json={"segments": [{"lo": 0, "hi": 10, "count": 5}]},
    )
    assert ok.status_code == 201
    # 冲突批次在修正后也能正常提交。
    retry = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 0, "hi": 10, "count": 10}]},
    )
    assert retry.status_code == 201
    assert client.get("/inspections/1").json()["report_count"] == 1


def test_409_schema_present_in_openapi(client):
    spec = client.get("/openapi.json").json()
    report_op = spec["paths"]["/inspections/{inspection_id}/reports"]["post"]
    assert "409" in report_op["responses"]
    assert "RejectionDetail" in spec["components"]["schemas"]


def test_409_evidence_uses_data_at_rejection_time(client):
    """拒收回执中的计数是判定当时的快照；之后数据更正也不会改写它。

    场景：旧报告登记 [0,50) 60 片。本次补报 [60,70) 101 片（与旧区间不
    重叠，仅因超过登记总片数 100 而冲突），回执引用本次表与总片数 100。
    随后把总片数更正为 120（数据变化），再制造一次拒收；两份回执各自
    保留自己判定时刻看到的数值。
    """
    make_inspection(client, 100)
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 0, "hi": 50, "count": 60}]},
        ).status_code
        == 201
    )

    first_409 = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 60, "hi": 70, "count": 101}]},
    )
    assert first_409.status_code == 409
    first_detail = first_409.json()["detail"]
    _assert_chain_well_formed(first_detail)
    first_total = next(
        step
        for step in first_detail["contradiction_chain"]["steps"]
        if step["basis"]["kind"] == "total"
    )["basis"]["total_pieces"]
    assert first_total == 100  # 判定时刻登记的总片数
    assert ("本次补报", 1) in {
        (row["source"], row["row_number"]) for row in first_detail["recheck_rows"]
    }

    # 更正总片数（与已有计数相容才允许），数据随后发生变化。
    assert (
        client.patch("/inspections/1", json={"total_pieces": 120}).status_code
        == 200
    )
    # 再用超总片数的补报触发拒收，回执引用的是更正后的总片数 120。
    second_409 = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 80, "hi": 90, "count": 121}]},
    )
    assert second_409.status_code == 409
    second_detail = second_409.json()["detail"]
    _assert_chain_well_formed(second_detail)
    total_step = next(
        step
        for step in second_detail["contradiction_chain"]["steps"]
        if step["basis"]["kind"] == "total"
    )
    assert total_step["basis"]["total_pieces"] == 120
    # 第一份回执是当时算出来的独立快照，不随后续更正而变化。
    assert first_detail != second_detail
    assert first_total == 100
