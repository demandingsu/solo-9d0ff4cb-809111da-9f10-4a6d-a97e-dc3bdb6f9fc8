"""SQLite 持久化层。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    total_pieces  INTEGER NOT NULL CHECK (total_pieces >= 0),
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS count_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id      INTEGER NOT NULL REFERENCES count_reports(id) ON DELETE CASCADE,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    lo             INTEGER NOT NULL CHECK (lo >= 0),
    hi             INTEGER NOT NULL CHECK (hi > lo),
    count          INTEGER NOT NULL CHECK (count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_reports_inspection
    ON count_reports(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_inspection
    ON segments(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_report
    ON segments(report_id);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开一个请求级连接。

    ``isolation_level=None`` 让我们显式使用 ``BEGIN IMMEDIATE`` 控制写事务，
    从而把“读取现有计数 + 校验 + 写入”放在同一个串行化事务里。

    ``check_same_thread=False``：每条连接只服务一个请求，但依赖清理与
    端点可能落在不同的线程池线程上；连接不会跨请求共享。
    """
    connection = sqlite3.connect(
        path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
    finally:
        connection.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_inspection(
    connection: sqlite3.Connection, total_pieces: int, note: str | None
) -> int:
    cursor = connection.execute(
        "INSERT INTO inspections (total_pieces, note, created_at) VALUES (?, ?, ?)",
        (total_pieces, note, now()),
    )
    return int(cursor.lastrowid)


def get_inspection(connection: sqlite3.Connection, inspection_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count
        FROM inspections i
        WHERE i.id = ?
        """,
        (inspection_id,),
    ).fetchone()


def list_inspections(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count
        FROM inspections i
        ORDER BY i.id
        """
    ).fetchall()


def update_inspection(
    connection: sqlite3.Connection,
    inspection_id: int,
    total_pieces: int,
    note: str | None,
) -> None:
    connection.execute(
        "UPDATE inspections SET total_pieces = ?, note = ? WHERE id = ?",
        (total_pieces, note, inspection_id),
    )


def load_labeled_constraints(
    connection: sqlite3.Connection, inspection_id: int
) -> list[dict]:
    """加载已接收区间并附出处：报告编号 + 该报告内的区间行号（从 1 起）。

    拒收判定必须使用这条带出处的数据，矛盾链才能逐行回查。
    """
    rows = connection.execute(
        """
        SELECT report_id, lo, hi, count,
               ROW_NUMBER() OVER (
                   PARTITION BY report_id ORDER BY id
               ) AS row_number
        FROM segments
        WHERE inspection_id = ?
        ORDER BY id
        """,
        (inspection_id,),
    ).fetchall()
    return [
        {
            "report_id": int(row["report_id"]),
            "row_number": int(row["row_number"]),
            "lo": int(row["lo"]),
            "hi": int(row["hi"]),
            "count": int(row["count"]),
        }
        for row in rows
    ]


def insert_report(
    connection: sqlite3.Connection,
    inspection_id: int,
    segments: Sequence[tuple[int, int, int]],
) -> int:
    cursor = connection.execute(
        "INSERT INTO count_reports (inspection_id, created_at) VALUES (?, ?)",
        (inspection_id, now()),
    )
    report_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO segments (report_id, inspection_id, lo, hi, count)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(report_id, inspection_id, lo, hi, count) for lo, hi, count in segments],
    )
    return report_id


def get_reports(connection: sqlite3.Connection, inspection_id: int) -> list[dict]:
    reports = connection.execute(
        "SELECT id, inspection_id, created_at FROM count_reports "
        "WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()
    segment_rows = connection.execute(
        "SELECT id, report_id, lo, hi, count FROM segments "
        "WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()

    segments_by_report: dict[int, list[dict]] = {int(r["id"]): [] for r in reports}
    for row in segment_rows:
        segments_by_report[int(row["report_id"])].append(
            {
                "id": int(row["id"]),
                "lo": int(row["lo"]),
                "hi": int(row["hi"]),
                "count": int(row["count"]),
            }
        )

    return [
        {
            "id": int(report["id"]),
            "inspection_id": int(report["inspection_id"]),
            "created_at": report["created_at"],
            "segments": segments_by_report[int(report["id"])],
        }
        for report in reports
    ]
