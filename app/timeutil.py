"""时间解析、格式化与线性漂移映射工具。

约定：
- 所有时间精度均为毫秒；微秒位不是 1000 整数倍的输入直接拒绝（不静默截断）。
- UTC 时间必须带时区信息（Z 或 ±HH:MM 偏移）。
- 设备本地时间可以带时区，也可以不带（naive）；同一台设备的锚点必须保持
  统一的时区形态（统一 naive，或统一带相同偏移），否则视为非法配置。
- 线性插值一律使用「设备本地墙上钟毫秒」（local_ms）：对带时区的时间先剥离
  偏移、保留墙上钟数值，避免不同偏移下的换算歧义。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence


class TimeFormatError(ValueError):
    """时间字符串非法或精度不符合毫秒约定。"""


def parse_time(value: str, *, require_tz: bool = False, field: str = "time") -> datetime:
    """解析 ISO 8601 时间字符串，要求毫秒精度。

    支持结尾 ``Z``；``require_tz`` 为真时拒绝不带时区的时间。
    """
    if not isinstance(value, str) or not value.strip():
        raise TimeFormatError(f"{field} 必须是 ISO 8601 字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimeFormatError(f"{field} 不是合法的 ISO 8601 时间: {value!r}") from exc
    if require_tz and dt.tzinfo is None:
        raise TimeFormatError(f"{field} 必须带时区信息（UTC 时间应以 Z 结尾）: {value!r}")
    if dt.microsecond and dt.microsecond % 1000 != 0:
        raise TimeFormatError(f"{field} 只支持毫秒精度: {value!r}")
    return dt


def _wall_naive(dt: datetime) -> datetime:
    """剥离时区，得到该时间在其本地墙上钟上的 naive 值。"""
    if dt.tzinfo is None:
        return dt
    return dt.replace(tzinfo=None)


def dt_to_ms(dt: datetime) -> int:
    """转换为整数 Unix 毫秒（naive 按字面值，aware 按绝对 UTC）。"""
    epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo)
    return int((dt - epoch).total_seconds() * 1000)


def local_wall_ms(dt: datetime) -> int:
    """设备本地墙上钟毫秒：aware 时间先剥离偏移再按字面值计数。"""
    naive = _wall_naive(dt)
    return int((naive - datetime(1970, 1, 1)).total_seconds() * 1000)


def offset_minutes(dt: datetime) -> Optional[int]:
    """时区偏移（分钟）；naive 返回 None。"""
    if dt.tzinfo is None:
        return None
    delta = dt.utcoffset()
    return int(delta.total_seconds() // 60)


def ms_to_dt(ms: int, tz: Optional[timezone] = None) -> datetime:
    """整数 Unix 毫秒转换回 datetime；tz 为 None 时得到 naive UTC 字面值。"""
    return datetime.fromtimestamp(ms / 1000, tz=tz)


def format_dt(dt: datetime) -> str:
    """规范输出：毫秒精度；aware 时间转成 UTC 并以 Z 结尾，naive 时间不加后缀。"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    ms = dt.microsecond // 1000
    head = dt.strftime("%Y-%m-%dT%H:%M:%S")
    suffix = "Z" if dt.tzinfo is not None else ""
    return f"{head}.{ms:03d}{suffix}"


def format_local(dt: datetime) -> str:
    """规范输出设备本地时间：保留其原有偏移（naive 则无后缀）。"""
    ms = dt.microsecond // 1000
    head = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        return f"{head}.{ms:03d}"
    offset = dt.strftime("%z")  # 形如 +0800
    return f"{head}.{ms:03d}{offset[:3]}:{offset[3:]}"


def utc_now_ms() -> int:
    """当前 UTC 时间的整数毫秒。"""
    return dt_to_ms(datetime.now(timezone.utc))


def interpolate_utc(local_ms: int, anchor_local_ms: int, anchor_utc_ms: int,
                    local_span_ms: int, utc_span_ms: int) -> int:
    """相邻锚点间的线性漂移映射（半开区间），返回 UTC 毫秒。

    入参全部为整数毫秒，结果四舍五入到整毫秒（对正负漂移均正确）。
    """
    numerator = (local_ms - anchor_local_ms) * utc_span_ms
    # Python 的 // 为 floor 除法：(2n+d)//(2d) 对正负 n 均等价于 half-up 四舍五入
    return anchor_utc_ms + (2 * numerator + local_span_ms) // (2 * local_span_ms)


@dataclass(frozen=True)
class AnchorPoint:
    """一个同步锚点：本地墙上钟毫秒 -> UTC 绝对毫秒（含时区形态标记）。"""
    local_ms: int
    utc_ms: int
    local_aware: bool
    offset_minutes: Optional[int] = None


def validate_chain(points: Sequence[tuple[datetime, datetime]]) -> list[AnchorPoint]:
    """校验一批锚点（本地时间, UTC 时间）并生成排序后的 AnchorPoint 列表。

    - 本地时间必须严格递增（按墙上钟）；
    - 按本地时间排序后 UTC 时间必须严格递增（锚点不能倒挂）；
    - 同一台设备的本地时间必须统一 naive 或统一带时区；带时区时偏移必须相同。
    """
    chain: list[AnchorPoint] = []
    for local_dt, utc_dt in points:
        chain.append(AnchorPoint(
            local_ms=local_wall_ms(local_dt),
            utc_ms=dt_to_ms(utc_dt.astimezone(timezone.utc)),
            local_aware=local_dt.tzinfo is not None,
            offset_minutes=offset_minutes(local_dt),
        ))
    chain.sort(key=lambda p: p.local_ms)

    awareness = {p.local_aware for p in chain}
    if len(awareness) > 1:
        raise ValueError("同一设备的锚点本地时间必须统一带时区或统一不带时区")
    offsets = {p.offset_minutes for p in chain if p.local_aware}
    if len(offsets) > 1:
        raise ValueError("同一设备带时区的锚点必须使用相同的 UTC 偏移")

    prev_local: Optional[int] = None
    prev_utc: Optional[int] = None
    for p in chain:
        if prev_local is not None:
            if p.local_ms <= prev_local:
                raise ValueError("锚点本地时间必须严格递增")
            if p.utc_ms <= prev_utc:
                raise ValueError("锚点 UTC 时间必须严格递增（存在倒挂锚点）")
        prev_local, prev_utc = p.local_ms, p.utc_ms
    return chain


def map_point(ms: int, chain: Sequence[AnchorPoint]) -> Optional[int]:
    """把一个本地墙上钟毫秒点按相邻锚点的线性漂移映射为 UTC 毫秒。

    - 恰好等于某个锚点：直接取该锚点的 UTC；
    - 落在两个锚点之间：线性插值（半开区间）；
    - 落在锚点覆盖范围外（早于首个 / 晚于末个）：返回 None。
    """
    if not chain:
        return None
    first, last = chain[0], chain[-1]
    if ms < first.local_ms or ms > last.local_ms:
        return None
    for left, right in zip(chain, chain[1:]):
        if left.local_ms <= ms < right.local_ms:
            return interpolate_utc(
                ms, left.local_ms, left.utc_ms,
                right.local_ms - left.local_ms,
                right.utc_ms - left.utc_ms,
            )
    return last.utc_ms
