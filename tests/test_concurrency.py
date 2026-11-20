"""并发补报测试：串行化事务保证不丢失不相容检查。"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


def test_concurrent_conflicting_reports_only_one_wins(tmp_path):
    app = create_app(str(tmp_path / "concurrent.db"))
    with TestClient(app) as client:
        assert client.post("/inspections", json={"total_pieces": 10}).status_code == 201

        def submit(count: int):
            return client.post(
                "/inspections/1/reports",
                json={"segments": [{"lo": 0, "hi": 10, "count": count}]},
            ).status_code

        # 5 与 6 互相矛盾，只能有一个成功。
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(submit, [5, 6]))

        assert statuses == [201, 409]
        detail = client.get("/inspections/1").json()
        assert detail["report_count"] == 1
        assert detail["segment_count"] == 1


def test_concurrent_compatible_reports_all_accepted(tmp_path):
    app = create_app(str(tmp_path / "concurrent2.db"))
    with TestClient(app) as client:
        assert client.post("/inspections", json={"total_pieces": 10}).status_code == 201

        def submit(pair):
            lo, hi = pair
            return client.post(
                "/inspections/1/reports",
                json={"segments": [{"lo": lo, "hi": hi, "count": 2}]},
            ).status_code

        pairs = [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            statuses = list(pool.map(submit, pairs))

        assert statuses == [201] * 5
        detail = client.get("/inspections/1").json()
        assert detail["report_count"] == 5
        bounds = client.get(
            "/inspections/1/bounds", params={"qualified_lo": 0, "qualified_hi": 10}
        ).json()
        assert bounds["min_count"] == 10
        assert bounds["max_count"] == 10


def test_conflict_in_one_batch_does_not_block_other_batches(tmp_path):
    """一个批次冲突整份退回时，其他批次的相容补报照常接收。"""
    app = create_app(str(tmp_path / "concurrent3.db"))
    with TestClient(app) as client:
        assert (
            client.post("/inspections", json={"total_pieces": 10}).status_code
            == 201
        )
        assert (
            client.post("/inspections", json={"total_pieces": 10}).status_code
            == 201
        )

        def submit(pair):
            inspection_id, count = pair
            return (
                inspection_id,
                client.post(
                    f"/inspections/{inspection_id}/reports",
                    json={"segments": [{"lo": 0, "hi": 10, "count": count}]},
                ).status_code,
            )

        # 批次 1 同时收到 6 和 7（互不相容），批次 2 收到相容的 3。
        jobs = [(1, 6), (1, 7), (2, 3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(submit, jobs))

        batch1 = sorted(code for iid, code in results if iid == 1)
        batch2 = [code for iid, code in results if iid == 2]
        assert batch1 == [201, 409]
        assert batch2 == [201]

        detail1 = client.get("/inspections/1").json()
        detail2 = client.get("/inspections/2").json()
        assert detail1["report_count"] == 1
        assert detail2["report_count"] == 1
