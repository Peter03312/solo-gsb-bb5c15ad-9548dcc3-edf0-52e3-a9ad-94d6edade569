"""业务服务层：设备注册、锚点追加/更正、声段提交、时间轴发布。

所有写操作都在 BEGIN IMMEDIATE 事务中完成，保证「锚点更正」与「时间轴发布」
并发时，每个发布版本对应同一时刻的一套完整锚点与声段快照。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timezone
from typing import Any, Optional

from . import db as dbmod
from .alignment import build_entries, diff_entries, serialize_entry
from .timeutil import (
    AnchorPoint,
    TimeFormatError,
    dt_to_ms,
    format_dt,
    format_local,
    local_wall_ms,
    ms_to_dt,
    offset_minutes,
    parse_time,
    utc_now_ms,
    validate_chain,
)


class ApiError(Exception):
    """业务错误：携带 HTTP 状态码、错误码与详情。"""

    def __init__(self, status_code: int, code: str, message: str,
                 details: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------- 设备

def register_device(conn: sqlite3.Connection, device_id: str,
                    name: Optional[str] = None) -> dict[str, Any]:
    device_id = (device_id or "").strip()
    if not device_id:
        raise ApiError(400, "INVALID_DEVICE", "设备编号不能为空")
    with dbmod.transaction(conn):
        row = conn.execute(
            "SELECT device_id, name, created_at_ms FROM devices WHERE device_id=?",
            (device_id,),
        ).fetchone()
        if row is not None:
            return {"device_id": row["device_id"], "name": row["name"], "registered": False}
        conn.execute(
            "INSERT INTO devices(device_id, name, created_at_ms) VALUES(?,?,?)",
            (device_id, name, utc_now_ms()),
        )
    return {"device_id": device_id, "name": name, "registered": True}


def _require_device(conn: sqlite3.Connection, device_id: str) -> None:
    row = conn.execute("SELECT 1 FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if row is None:
        raise ApiError(404, "UNKNOWN_DEVICE", f"未知设备: {device_id}",
                       {"device_id": device_id})


# ---------------------------------------------------------------- 锚点

def _parse_anchor(item: Any) -> tuple:
    """解析单个锚点 JSON -> (local_dt, utc_dt, local_str, utc_str, offset)。"""
    if not isinstance(item, dict):
        raise ApiError(400, "INVALID_ANCHOR", "每个锚点必须是对象")
    if "local_time" not in item or "utc_time" not in item:
        raise ApiError(400, "INVALID_ANCHOR",
                       "锚点必须包含 local_time 和 utc_time 字段", {"item": item})
    try:
        local_dt = parse_time(item["local_time"], field="local_time")
        utc_dt = parse_time(item["utc_time"], require_tz=True, field="utc_time")
    except TimeFormatError as exc:
        raise ApiError(400, "INVALID_TIME", str(exc), {"item": item}) from exc
    return (local_dt, utc_dt, format_local(local_dt), format_dt(utc_dt),
            offset_minutes(local_dt))


def add_anchors(conn: sqlite3.Connection, device_id: str,
                payload: Any) -> dict[str, Any]:
    """增量录入同步锚点：与已有锚点合并后必须整体严格递增，否则拒绝写入。"""
    device_id = (device_id or "").strip()
    if not device_id:
        raise ApiError(400, "INVALID_DEVICE", "设备编号不能为空")

    items = payload if isinstance(payload, list) else [payload]
    if not items:
        raise ApiError(400, "INVALID_ANCHOR", "至少提交一个锚点")
    parsed = [_parse_anchor(item) for item in items]

    with dbmod.transaction(conn):
        _require_device(conn, device_id)

        existing = conn.execute(
            "SELECT local_time, local_ms, utc_ms, offset_minutes FROM anchors "
            "WHERE device_id=? ORDER BY seq", (device_id,),
        ).fetchall()

        # 新锚点先自检严格递增（也校验偏移一致）
        try:
            validate_chain([(p[0], p[1]) for p in parsed])
        except ValueError as exc:
            raise ApiError(422, "ANCHOR_NOT_STRICTLY_INCREASING", str(exc)) from exc

        # 与既有锚点的时区形态必须一致
        if existing:
            ex_aware = existing[0]["offset_minutes"] is not None
            new_aware = parsed[0][4] is not None
            if ex_aware != new_aware:
                raise ApiError(400, "INVALID_ANCHOR",
                               "新锚点本地时间的时区形态与既有锚点不一致")
            if ex_aware and existing[0]["offset_minutes"] != parsed[0][4]:
                raise ApiError(400, "INVALID_ANCHOR",
                               "新锚点的 UTC 偏移与既有锚点不一致")

        # 从存储的原始本地时间字符串解析，完整保留原时区偏移后合并校验
        merged = [
            (parse_time(r["local_time"], field="local_time"),
             ms_to_dt(r["utc_ms"], tz=timezone.utc))
            for r in existing
        ]
        merged.extend((p[0], p[1]) for p in parsed)
        try:
            full_chain = validate_chain(merged)
        except ValueError as exc:
            raise ApiError(422, "ANCHOR_NOT_STRICTLY_INCREASING", str(exc)) from exc

        # 以合并后链的顺序确定 seq；local_ms 主键冲突即重复锚点
        seq_of = {p.local_ms: idx for idx, p in enumerate(full_chain)}
        inserted = 0
        for local_dt, _utc_dt, local_str, utc_str, off in parsed:
            lms = local_wall_ms(local_dt)
            try:
                conn.execute(
                    "INSERT INTO anchors(device_id, local_time, local_ms, utc_time, "
                    "utc_ms, offset_minutes, seq) VALUES(?,?,?,?,?,?,?)",
                    (device_id, local_str, lms, utc_str,
                     dt_to_ms(_utc_dt.astimezone(timezone.utc)), off, seq_of[lms]),
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "ANCHOR_CONFLICT",
                               "该本地时刻的锚点已存在，禁止重复写入",
                               {"local_time": local_str}) from exc
            inserted += 1

        for p in full_chain:
            conn.execute(
                "UPDATE anchors SET seq=? WHERE device_id=? AND local_ms=?",
                (seq_of[p.local_ms], device_id, p.local_ms),
            )

        total = conn.execute(
            "SELECT COUNT(*) AS c FROM anchors WHERE device_id=?", (device_id,),
        ).fetchone()["c"]

    return {"device_id": device_id, "inserted": inserted, "total": total}


def replace_anchors(conn: sqlite3.Connection, device_id: str,
                    payload: Any) -> dict[str, Any]:
    """锚点更正：用提交的完整锚点集原子替换该设备的全部锚点。

    已发布版本的锚点快照不受影响；更正后声段在当前（草稿）时间轴立即重算，
    下一次发布生成新版本并在版本差异中体现位移。
    """
    device_id = (device_id or "").strip()
    if not device_id:
        raise ApiError(400, "INVALID_DEVICE", "设备编号不能为空")

    items = payload if isinstance(payload, list) else [payload]
    parsed = [_parse_anchor(item) for item in items]
    try:
        chain = validate_chain([(p[0], p[1]) for p in parsed])
    except ValueError as exc:
        raise ApiError(422, "ANCHOR_NOT_STRICTLY_INCREASING", str(exc)) from exc

    with dbmod.transaction(conn):
        _require_device(conn, device_id)
        conn.execute("DELETE FROM anchors WHERE device_id=?", (device_id,))
        seq_of = {p.local_ms: idx for idx, p in enumerate(chain)}
        for local_dt, utc_dt, local_str, utc_str, off in parsed:
            lms = local_wall_ms(local_dt)
            conn.execute(
                "INSERT INTO anchors(device_id, local_time, local_ms, utc_time, "
                "utc_ms, offset_minutes, seq) VALUES(?,?,?,?,?,?,?)",
                (device_id, local_str, lms, utc_str,
                 dt_to_ms(utc_dt.astimezone(timezone.utc)), off, seq_of[lms]),
            )
        total = len(parsed)
    return {"device_id": device_id, "replaced": True, "total": total}


# ---------------------------------------------------------------- 声段

def _segment_content_hash(device_id: str, local_start: str, local_end: str) -> str:
    body = json.dumps(
        {"device_id": device_id, "local_start": local_start, "local_end": local_end},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _normalize_segment(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ApiError(400, "INVALID_SEGMENT", "每个声段必须是对象")
    cid = item.get("collection_id")
    device_id = item.get("device_id")
    cid = cid.strip() if isinstance(cid, str) else ""
    device_id = device_id.strip() if isinstance(device_id, str) else ""
    if not cid:
        raise ApiError(400, "INVALID_SEGMENT", "collection_id 不能为空", {"item": item})
    if not device_id:
        raise ApiError(400, "INVALID_SEGMENT", "device_id 不能为空", {"item": item})
    try:
        start_dt = parse_time(item["local_start"], field="local_start")
        end_dt = parse_time(item["local_end"], field="local_end")
    except TimeFormatError as exc:
        raise ApiError(400, "INVALID_TIME", str(exc), {"item": item}) from exc

    if (start_dt.tzinfo is None) != (end_dt.tzinfo is None):
        raise ApiError(400, "INVALID_TIME",
                       "声段起止时间必须同时带时区或同时不带时区",
                       {"collection_id": cid})
    if (start_dt.tzinfo is not None
            and offset_minutes(start_dt) != offset_minutes(end_dt)):
        raise ApiError(400, "INVALID_TIME",
                       "声段起止时间的 UTC 偏移必须相同", {"collection_id": cid})
    if local_wall_ms(start_dt) >= local_wall_ms(end_dt):
        raise ApiError(422, "SEGMENT_RANGE_INVALID",
                       "声段起止倒置：local_start 必须严格早于 local_end",
                       {"collection_id": cid,
                        "local_start": item["local_start"],
                        "local_end": item["local_end"]})

    local_start, local_end = format_local(start_dt), format_local(end_dt)
    return {
        "collection_id": cid,
        "device_id": device_id,
        "local_start": local_start,
        "local_end": local_end,
        "start_ms": local_wall_ms(start_dt),
        "end_ms": local_wall_ms(end_dt),
        "start_aware": start_dt.tzinfo is not None,
        "end_aware": end_dt.tzinfo is not None,
        "offset_minutes": offset_minutes(start_dt),
        "content_hash": _segment_content_hash(device_id, local_start, local_end),
    }


def upsert_segments(conn: sqlite3.Connection, payload: Any) -> dict[str, Any]:
    """提交一个或一批声段（全批原子，任一非法则整批不写入）。

    - 同 collection_id 同内容 => 幂等，计入 idempotent；
    - 同 collection_id 异内容 => 409 冲突，整批拒绝。
    """
    items = payload if isinstance(payload, list) else [payload]
    if not items:
        raise ApiError(400, "INVALID_SEGMENT", "至少提交一个声段")

    normalized = [_normalize_segment(item) for item in items]

    # 批内重复：同内容去重（视为一次重传），异内容直接冲突
    seen: dict[str, dict[str, Any]] = {}
    for seg in normalized:
        cid = seg["collection_id"]
        if cid in seen and seen[cid]["content_hash"] != seg["content_hash"]:
            raise ApiError(409, "SEGMENT_CONTENT_CONFLICT",
                           f"批内采集编号 {cid} 出现不同内容",
                           {"collection_id": cid})
        seen[cid] = seg
    batch = list(seen.values())

    inserted: list[str] = []
    idempotent: list[str] = []
    conflicts: list[dict[str, Any]] = []

    with dbmod.transaction(conn):
        # 全部设备必须已知；未知设备不写入任何内容
        device_ids = {seg["device_id"] for seg in batch}
        placeholders = ",".join("?" * len(device_ids))
        known = {
            r["device_id"] for r in conn.execute(
                f"SELECT device_id FROM devices WHERE device_id IN ({placeholders})",
                tuple(device_ids),
            )
        }
        unknown = sorted(device_ids - known)
        if unknown:
            raise ApiError(404, "UNKNOWN_DEVICE",
                           f"未知设备: {', '.join(unknown)}", {"devices": unknown})

        for seg in batch:
            row = conn.execute(
                "SELECT device_id, content_hash FROM segments WHERE collection_id=?",
                (seg["collection_id"],),
            ).fetchone()
            if row is not None:
                if row["content_hash"] == seg["content_hash"]:
                    idempotent.append(seg["collection_id"])
                    continue
                conflicts.append({
                    "collection_id": seg["collection_id"],
                    "existing_device_id": row["device_id"],
                    "submitted_device_id": seg["device_id"],
                })
                continue
            conn.execute(
                "INSERT INTO segments(collection_id, device_id, local_start, local_end, "
                "start_ms, end_ms, start_aware, end_aware, offset_minutes, "
                "content_hash, created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (seg["collection_id"], seg["device_id"], seg["local_start"],
                 seg["local_end"], seg["start_ms"], seg["end_ms"],
                 int(seg["start_aware"]), int(seg["end_aware"]),
                 seg["offset_minutes"], seg["content_hash"], utc_now_ms()),
            )
            inserted.append(seg["collection_id"])

        if conflicts:
            raise ApiError(409, "SEGMENT_CONTENT_CONFLICT",
                           "存在相同采集编号但内容不同的声段，整批已拒绝写入",
                           {"conflicts": conflicts})

    return {
        "received": len(items),
        "inserted": len(inserted),
        "idempotent": len(idempotent),
        "collection_ids": inserted + idempotent,
    }


# ---------------------------------------------------------------- 快照读取

def _load_chains(conn: sqlite3.Connection) -> dict[str, list[AnchorPoint]]:
    chains: dict[str, list[AnchorPoint]] = {}
    rows = conn.execute(
        "SELECT device_id, local_ms, utc_ms, offset_minutes "
        "FROM anchors ORDER BY device_id, seq"
    ).fetchall()
    for r in rows:
        aware = r["offset_minutes"] is not None
        chains.setdefault(r["device_id"], []).append(
            AnchorPoint(r["local_ms"], r["utc_ms"], aware, r["offset_minutes"])
        )
    return chains


def _load_segments(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT collection_id, device_id, local_start, local_end, start_ms, end_ms, "
        "start_aware, end_aware, offset_minutes FROM segments"
    ).fetchall()
    return [dict(r) for r in rows]


def _load_anchor_snapshot(conn: sqlite3.Connection) -> list[dict]:
    snapshot: dict[str, list[dict]] = {}
    rows = conn.execute(
        "SELECT device_id, seq, local_time, utc_time "
        "FROM anchors ORDER BY device_id, seq"
    ).fetchall()
    for r in rows:
        snapshot.setdefault(r["device_id"], []).append({
            "seq": r["seq"],
            "local_time": r["local_time"],
            "utc_time": r["utc_time"],
        })
    return [{"device_id": d, "anchors": snapshot[d]} for d in sorted(snapshot)]


def current_alignment(conn: sqlite3.Connection,
                      device_id: Optional[str] = None) -> dict[str, Any]:
    """当前（草稿）时间轴：用最新锚点即时重算，未发布也可查看。"""
    if device_id:
        device_id = device_id.strip()
        _require_device(conn, device_id)
    segments = _load_segments(conn)
    if device_id:
        segments = [s for s in segments if s["device_id"] == device_id]
    chains = _load_chains(conn)
    entries = build_entries(segments, chains)
    return {"state": "draft", "segments": [serialize_entry(e) for e in entries]}


# ---------------------------------------------------------------- 发布

_INTERNAL_KEYS = (
    "collection_id", "device_id", "local_start", "local_end",
    "status", "reason", "utc_start", "utc_end",
)


def publish_timeline(conn: sqlite3.Connection) -> dict[str, Any]:
    """发布一个不可变新版本：快照当前全部锚点与声段，并与上一版求差异。"""
    with dbmod.transaction(conn):
        chains = _load_chains(conn)
        segments = _load_segments(conn)
        entries = build_entries(segments, chains)
        anchor_snapshot = _load_anchor_snapshot(conn)

        prev_row = conn.execute(
            "SELECT version, entries_json FROM timeline_versions "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()

        entries_payload = [serialize_entry(e) for e in entries]
        # 快照内部形态，供后续版本差异比较
        entries_internal = [{k: e[k] for k in _INTERNAL_KEYS} for e in entries]
        cur = conn.execute(
            "INSERT INTO timeline_versions(published_at_ms, entries_json, anchors_json) "
            "VALUES(?,?,?)",
            (utc_now_ms(),
             json.dumps({"entries": entries_internal}, ensure_ascii=False),
             json.dumps(anchor_snapshot, ensure_ascii=False)),
        )
        version = cur.lastrowid

        if prev_row is None:
            changes = {"added": entries_payload, "removed": [], "displaced": []}
            prev_version = None
        else:
            prev_version = prev_row["version"]
            old_entries = json.loads(prev_row["entries_json"])["entries"]
            changes = diff_entries(old_entries, entries_internal)

    return {
        "version": version,
        "previous_version": prev_version,
        "changes": changes,
        "segments": entries_payload,
        "anchors": anchor_snapshot,
    }


def list_versions(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT version, published_at_ms, entries_json FROM timeline_versions "
        "ORDER BY version"
    ).fetchall()
    versions = [{
        "version": r["version"],
        "published_at": format_dt(ms_to_dt(r["published_at_ms"], tz=timezone.utc)),
        "segment_count": len(json.loads(r["entries_json"])["entries"]),
    } for r in rows]
    return {"versions": versions}


def get_version(conn: sqlite3.Connection, version: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT version, published_at_ms, entries_json, anchors_json "
        "FROM timeline_versions WHERE version=?", (version,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "VERSION_NOT_FOUND", f"时间轴版本不存在: v{version}",
                       {"version": version})
    internal = json.loads(row["entries_json"])["entries"]
    anchors = json.loads(row["anchors_json"])
    return {
        "version": row["version"],
        "published_at": format_dt(ms_to_dt(row["published_at_ms"], tz=timezone.utc)),
        "segments": [serialize_entry(e) for e in internal],
        "anchors": anchors,
    }
