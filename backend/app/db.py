"""SQLite 持久化：项目与求解结果。

结果为 JSON 大对象，直接以 TEXT 存储，避免过度建表。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

DB_PATH = os.environ.get("KNIT_DB_PATH", "/data/knit.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
-- 试织纠偏修订：绑定到“生成它的那一次求解结果”（result_id 不随项目重新求解改变）
CREATE TABLE IF NOT EXISTS revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    result_id   INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    note        TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (result_id) REFERENCES results(id) ON DELETE CASCADE
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)


def list_projects() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, updated_at FROM projects ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def create_project(payload: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, payload, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (payload.get("name", "未命名项目"), json.dumps(payload, ensure_ascii=False), now, now),
        )
        pid = cur.lastrowid
    return {"id": pid, **_project_summary(pid)}


def _project_summary(pid: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, created_at, updated_at FROM projects WHERE id = ?", (pid,)
        ).fetchone()
        return dict(row)


def get_project(pid: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d


def update_project(pid: int, payload: dict[str, Any]) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE projects SET name = ?, payload = ?, updated_at = ? WHERE id = ?",
            (payload.get("name", "未命名项目"), json.dumps(payload, ensure_ascii=False), time.time(), pid),
        )
        return cur.rowcount > 0


def delete_project(pid: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        return cur.rowcount > 0


def save_result(project_id: int, payload: dict[str, Any]) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO results (project_id, payload, created_at) VALUES (?, ?, ?)",
            (project_id, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (time.time(), project_id))
        return int(cur.lastrowid)


def latest_result(project_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, payload, created_at FROM results WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d


def get_result(project_id: int, result_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, project_id, payload, created_at FROM results "
            "WHERE id = ? AND project_id = ?",
            (result_id, project_id),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d


def list_results(project_id: int) -> list[dict[str, Any]]:
    """项目的全部历史求解结果（新在前）；重新求解只追加，旧结果与其修订仍可查看。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at FROM results WHERE project_id = ? ORDER BY id DESC",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------- 试织纠偏修订（绑定求解结果） ----------------

def save_revision(project_id: int, result_id: int, payload: dict[str, Any],
                  note: str | None = None) -> int:
    with get_conn() as conn:
        # result_id 必须确实属于该项目
        own = conn.execute(
            "SELECT 1 FROM results WHERE id = ? AND project_id = ?",
            (result_id, project_id),
        ).fetchone()
        if own is None:
            raise ValueError("求解结果不属于该项目")
        cur = conn.execute(
            "INSERT INTO revisions (project_id, result_id, payload, created_at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, result_id, json.dumps(payload, ensure_ascii=False),
             time.time(), note),
        )
        return int(cur.lastrowid)


def list_revisions(project_id: int, result_id: int | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT id, result_id, payload, created_at, note FROM revisions "
           "WHERE project_id = ?")
    args: list[Any] = [project_id]
    if result_id is not None:
        sql += " AND result_id = ?"
        args.append(result_id)
    sql += " ORDER BY id DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out


def get_revision(project_id: int, revision_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, result_id, payload, created_at, note FROM revisions "
            "WHERE id = ? AND project_id = ?",
            (revision_id, project_id),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d


def delete_revision(project_id: int, revision_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM revisions WHERE id = ? AND project_id = ?",
            (revision_id, project_id),
        )
        return cur.rowcount > 0
