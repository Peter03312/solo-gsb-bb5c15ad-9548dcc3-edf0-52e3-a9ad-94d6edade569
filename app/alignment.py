"""声段定位、排序与版本差异计算（纯函数，方便单测）。"""
from __future__ import annotations

from datetime import timezone
from typing import Any, Iterable, Optional, Sequence

from .timeutil import AnchorPoint, dt_to_ms, format_dt, map_point, ms_to_dt, parse_time


def _ms_to_str(ms: int, aware: bool) -> str:
    """内部毫秒 -> 输出字符串。aware 按 UTC 输出并加 Z，naive 输出裸时间。"""
    return format_dt(ms_to_dt(ms, tz=timezone.utc if aware else None))


def locate_segment(
    segment: dict[str, Any],
    chain: Optional[Sequence[AnchorPoint]],
) -> dict[str, Any]:
    """按设备锚点链定位单个声段，返回带 status / utc 区间的条目。

    - 起止任一时刻落在锚点覆盖范围外 => unlocatable(reason=outside_anchor_coverage)
    - 声段本地时间的时区形态与锚点不一致 => unlocatable(reason=timezone_mismatch)
    """
    entry = dict(segment)
    if chain is None:
        entry.update(status="unlocatable", reason="outside_anchor_coverage",
                     utc_start=None, utc_end=None)
        return entry

    aware_chain = chain[0].local_aware
    seg_aware = bool(segment["start_aware"])
    if seg_aware != aware_chain:
        entry.update(status="unlocatable", reason="timezone_mismatch",
                     utc_start=None, utc_end=None)
        return entry
    # 声段与锚点都带时区时，UTC 偏移也必须一致
    if aware_chain and segment.get("offset_minutes") != chain[0].offset_minutes:
        entry.update(status="unlocatable", reason="timezone_mismatch",
                     utc_start=None, utc_end=None)
        return entry

    start_utc = map_point(segment["start_ms"], chain)
    end_utc = map_point(segment["end_ms"], chain)
    if start_utc is None or end_utc is None:
        entry.update(status="unlocatable", reason="outside_anchor_coverage",
                     utc_start=None, utc_end=None)
        return entry

    entry.update(status="locatable", reason=None,
                 utc_start=start_utc, utc_end=end_utc)
    return entry


def sort_key(entry: dict[str, Any]) -> tuple:
    """稳定排序：UTC 起点、设备编号、采集编号。

    无法定位的声段没有 UTC 起点，统一排在已定位声段之后。
    """
    located = entry["status"] == "locatable"
    return (
        0 if located else 1,
        entry.get("utc_start") if located else 0,
        entry["device_id"],
        entry["collection_id"],
    )


def build_entries(
    segments: Iterable[dict[str, Any]],
    chains: dict[str, Sequence[AnchorPoint]],
) -> list[dict[str, Any]]:
    """对全部声段执行定位并按稳定规则排序。"""
    entries = [locate_segment(seg, chains.get(seg["device_id"])) for seg in segments]
    entries.sort(key=sort_key)
    return entries


def serialize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """内部条目 -> 对外 JSON（内部毫秒字段不外泄，输出规范字符串）。"""
    located = entry["status"] == "locatable"
    payload: dict[str, Any] = {
        "collection_id": entry["collection_id"],
        "device_id": entry["device_id"],
        "local_start": entry["local_start"],
        "local_end": entry["local_end"],
        "status": entry["status"],
        "utc_start": _ms_to_str(entry["utc_start"], True) if located else None,
        "utc_end": _ms_to_str(entry["utc_end"], True) if located else None,
    }
    if not located:
        payload["reason"] = entry.get("reason")
    return payload


def _by_collection(entries: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["collection_id"]: e for e in entries}


def _sort_payload(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """对外条目按 (UTC 起点, 设备, 采集) 稳定排序。"""
    def key(row: dict[str, Any]) -> tuple:
        located = row["status"] == "locatable"
        utc_ms = (dt_to_ms(parse_time(row["utc_start"], require_tz=True))
                  if located and row.get("utc_start") else 0)
        return (0 if located else 1, utc_ms, row["device_id"], row["collection_id"])

    return sorted(rows, key=key)


def diff_entries(
    old: Sequence[dict[str, Any]],
    new: Sequence[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """对比相邻两个版本，列出新增、移除、位移声段；各列表按稳定规则排序。"""
    old_map, new_map = _by_collection(old), _by_collection(new)
    added = [serialize_entry(new_map[cid]) for cid in new_map.keys() - old_map.keys()]
    removed = [serialize_entry(old_map[cid]) for cid in old_map.keys() - new_map.keys()]

    displaced: list[dict[str, Any]] = []
    for cid in old_map.keys() & new_map.keys():
        o, n = old_map[cid], new_map[cid]
        if (o["status"], o.get("utc_start"), o.get("utc_end")) != (
            n["status"], n.get("utc_start"), n.get("utc_end")
        ):
            item = serialize_entry(n)
            item["previous"] = {
                "status": o["status"],
                "utc_start": _ms_to_str(o["utc_start"], True)
                if o["status"] == "locatable" else None,
                "utc_end": _ms_to_str(o["utc_end"], True)
                if o["status"] == "locatable" else None,
                "reason": o.get("reason") if o["status"] != "locatable" else None,
            }
            if n["status"] == "locatable" and o["status"] == "locatable":
                item["shift_ms"] = {
                    "start": n["utc_start"] - o["utc_start"],
                    "end": n["utc_end"] - o["utc_end"],
                }
            displaced.append(item)

    return {
        "added": _sort_payload(added),
        "removed": sorted(removed, key=lambda e: e["collection_id"]),
        "displaced": _sort_payload(displaced),
    }
