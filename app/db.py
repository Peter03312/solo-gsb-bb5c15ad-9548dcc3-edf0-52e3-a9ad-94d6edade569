"""SQLite 连接管理与建表初始化。

- WAL 模式 + busy timeout，保证「更正锚点」与「发布时间轴」并发时
  每个写事务都拿到一套完整一致的快照（单写入者序列化）。
- 每个工作线程/请求一个连接，FastAPI 的同步依赖通过 ``contextvars`` 复用。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.environ.get("ALIGNMENT_DB_PATH", "/data/alignment.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id     TEXT PRIMARY KEY,
    name          TEXT,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS anchors (
    device_id        TEXT NOT NULL REFERENCES devices(device_id),
    local_time       TEXT NOT NULL,   -- 规范后的设备本地墙上钟时间（毫秒，保留原偏移）
    local_ms         INTEGER NOT NULL,-- 本地墙上钟毫秒（剥离偏移后的字面值）
    utc_time         TEXT NOT NULL,   -- 规范后的 UTC 时间（毫秒，Z 结尾）
    utc_ms           INTEGER NOT NULL,
    offset_minutes   INTEGER,         -- 本地时间 UTC 偏移（分钟）；naive 为 NULL
    seq              INTEGER NOT NULL,-- 按本地时间排序的严格递增序号
    PRIMARY KEY (device_id, local_ms)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS segments (
    collection_id   TEXT NOT NULL,
    device_id       TEXT NOT NULL REFERENCES devices(device_id),
    local_start     TEXT NOT NULL,
    local_end       TEXT NOT NULL,
    start_ms        INTEGER NOT NULL,-- 本地墙上钟毫秒
    end_ms          INTEGER NOT NULL,
    start_aware     INTEGER NOT NULL,
    end_aware       INTEGER NOT NULL,
    offset_minutes  INTEGER,          -- 起止时间共同的 UTC 偏移（分钟）；naive 为 NULL
    content_hash    TEXT NOT NULL,    -- 幂等/冲突判定用的内容指纹
    created_at_ms   INTEGER NOT NULL,
    PRIMARY KEY (collection_id)
) WITHOUT ROWID;

-- 每个发布版本的元数据；entries_json / anchors_json 是该版本不可变的完整快照
CREATE TABLE IF NOT EXISTS timeline_versions (
    version        INTEGER PRIMARY KEY AUTOINCREMENT,
    published_at_ms INTEGER NOT NULL,
    entries_json   TEXT NOT NULL,
    anchors_json   TEXT NOT NULL
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即开始写事务（BEGIN IMMEDIATE），提交/回滚自动处理。

    BEGIN IMMEDIATE 一上来就获取保留锁，把「读快照 + 插入版本」整体串行化，
    从而保证每个版本对应同一时刻的一套完整锚点与声段。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(db_path: str | None = None) -> None:
    """幂等建表；容器启动时调用一次。"""
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()
