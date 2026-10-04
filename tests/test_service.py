"""单元/集成测试（标准库 unittest）。

运行：python3 -m unittest discover -s tests -v
使用每个用例独立的临时 SQLite 文件，不依赖运行中的服务。
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest

from app import db as dbmod
from app import services
from app.timeutil import (
    AnchorPoint,
    dt_to_ms,
    local_wall_ms,
    map_point,
    parse_time,
)


class TestTimeMath(unittest.TestCase):
    def _chain(self):
        a = parse_time("2026-10-04T18:00:00.000+08:00")
        b = parse_time("2026-10-04T19:00:00.000+08:00")
        ua = parse_time("2026-10-04T10:00:00.000Z", require_tz=True)
        ub = parse_time("2026-10-04T10:59:59.000Z", require_tz=True)
        return AnchorPoint(local_wall_ms(a), dt_to_ms(ua), True, 480), \
            AnchorPoint(local_wall_ms(b), dt_to_ms(ub), True, 480)

    def test_midpoint_drift(self):
        # 一小时内本地走 3600s，UTC 走 3599s；中点 UTC = 10:29:59.500
        first, last = self._chain()
        mid = first.local_ms + 1800_000
        self.assertEqual(
            map_point(mid, [first, last]),
            first.utc_ms + 1799_500,
        )

    def test_endpoint_exact_and_outside(self):
        first, last = self._chain()
        self.assertEqual(map_point(first.local_ms, [first, last]), first.utc_ms)
        self.assertEqual(map_point(last.local_ms, [first, last]), last.utc_ms)
        self.assertIsNone(map_point(first.local_ms - 1, [first, last]))
        self.assertIsNone(map_point(last.local_ms + 1, [first, last]))

    def test_empty_chain(self):
        self.assertIsNone(map_point(0, []))

    def test_negative_drift_rounding(self):
        # UTC 比本地快 1 秒/小时（正漂移），以及 UTC 比本地慢 1 秒/小时（负漂移）
        def chain(utc_end):
            a = parse_time("2026-10-04T10:00:00.000+00:00")
            b = parse_time("2026-10-04T11:00:00.000+00:00")
            ua = parse_time("2026-10-04T10:00:00.000Z", require_tz=True)
            ub = parse_time(utc_end, require_tz=True)
            return [AnchorPoint(local_wall_ms(a), dt_to_ms(ua), True, 0),
                    AnchorPoint(local_wall_ms(b), dt_to_ms(ub), True, 0)]

        probe = local_wall_ms(parse_time("2026-10-04T10:30:10.000+00:00"))
        fast = map_point(probe, chain("2026-10-04T11:00:01.000Z"))
        slow = map_point(probe, chain("2026-10-04T10:59:59.000Z"))
        base = dt_to_ms(parse_time("2026-10-04T10:30:10.000Z", require_tz=True))
        # 每小时漂移 +1s / -1s：30:10 处分别为 +503ms / -503ms
        self.assertEqual(fast - base, 503)
        self.assertEqual(slow - base, -503)

    def test_reject_sub_millisecond(self):
        from app.timeutil import TimeFormatError
        with self.assertRaises(TimeFormatError):
            parse_time("2026-10-04T10:00:00.0005Z", require_tz=True)

    def test_reject_naive_utc(self):
        from app.timeutil import TimeFormatError
        with self.assertRaises(TimeFormatError):
            parse_time("2026-10-04T10:00:00.000", require_tz=True)


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        dbmod.init_db(self.db_path)
        self.conn = dbmod.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def register(self, device_id="DEV-A", name=None):
        return services.register_device(self.conn, device_id, name)

    def add_anchors(self, device_id, anchors):
        return services.add_anchors(self.conn, device_id, anchors)

    def publish(self):
        return services.publish_timeline(self.conn)


class TestAnchorsAndSegments(ServiceTestBase):
    A_ANCHORS = [
        {"local_time": "2026-10-04T18:00:00.000+08:00",
         "utc_time": "2026-10-04T10:00:00.000Z"},
        {"local_time": "2026-10-04T19:00:00.000+08:00",
         "utc_time": "2026-10-04T10:59:59.000Z"},
    ]

    def test_register_and_duplicate_device(self):
        self.assertTrue(self.register("DEV-A")["registered"])
        self.assertFalse(self.register("DEV-A")["registered"])

    def test_unknown_device_anchor(self):
        with self.assertRaises(services.ApiError) as ctx:
            self.add_anchors("NOPE", self.A_ANCHORS)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_anchor_not_increasing_local(self):
        self.register()
        with self.assertRaises(services.ApiError) as ctx:
            self.add_anchors("DEV-A", [
                {"local_time": "2026-10-04T18:00:00.000+08:00",
                 "utc_time": "2026-10-04T10:00:00.000Z"},
                {"local_time": "2026-10-04T17:00:00.000+08:00",
                 "utc_time": "2026-10-04T11:00:00.000Z"},
            ])
        self.assertEqual(ctx.exception.status_code, 422)

    def test_anchor_utc_not_increasing(self):
        self.register()
        with self.assertRaises(services.ApiError) as ctx:
            self.add_anchors("DEV-A", [
                {"local_time": "2026-10-04T18:00:00.000+08:00",
                 "utc_time": "2026-10-04T11:00:00.000Z"},
                {"local_time": "2026-10-04T19:00:00.000+08:00",
                 "utc_time": "2026-10-04T10:00:00.000Z"},
            ])
        self.assertEqual(ctx.exception.status_code, 422)

    def test_mixed_offset_rejected(self):
        self.register()
        with self.assertRaises(services.ApiError):
            self.add_anchors("DEV-A", [
                {"local_time": "2026-10-04T18:00:00.000+08:00",
                 "utc_time": "2026-10-04T10:00:00.000Z"},
                {"local_time": "2026-10-04T19:00:00.000+09:00",
                 "utc_time": "2026-10-04T10:59:59.000Z"},
            ])

    def test_segment_reversed_rejected(self):
        self.register()
        self.add_anchors("DEV-A", self.A_ANCHORS)
        with self.assertRaises(services.ApiError) as ctx:
            services.upsert_segments(self.conn, {
                "collection_id": "SEG-1", "device_id": "DEV-A",
                "local_start": "2026-10-04T18:30:10.000+08:00",
                "local_end": "2026-10-04T18:30:00.000+08:00",
            })
        self.assertEqual(ctx.exception.status_code, 422)

    def test_unknown_device_segment_not_written(self):
        with self.assertRaises(services.ApiError) as ctx:
            services.upsert_segments(self.conn, {
                "collection_id": "SEG-1", "device_id": "GHOST",
                "local_start": "2026-10-04T18:30:00.000+08:00",
                "local_end": "2026-10-04T18:30:10.000+08:00",
            })
        self.assertEqual(ctx.exception.status_code, 404)
        count = self.conn.execute("SELECT COUNT(*) AS c FROM segments").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_idempotent_and_conflict(self):
        self.register()
        self.add_anchors("DEV-A", self.A_ANCHORS)
        seg = {
            "collection_id": "SEG-1", "device_id": "DEV-A",
            "local_start": "2026-10-04T18:30:00.000+08:00",
            "local_end": "2026-10-04T18:30:10.000+08:00",
        }
        first = services.upsert_segments(self.conn, seg)
        again = services.upsert_segments(self.conn, dict(seg))
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(again["idempotent"], 1)

        bad = dict(seg, local_end="2026-10-04T18:30:11.000+08:00")
        with self.assertRaises(services.ApiError) as ctx:
            services.upsert_segments(self.conn, bad)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_unlocatable_outside_coverage(self):
        self.register()
        self.add_anchors("DEV-A", self.A_ANCHORS)
        services.upsert_segments(self.conn, {
            "collection_id": "SEG-OUT", "device_id": "DEV-A",
            "local_start": "2026-10-04T19:30:00.000+08:00",
            "local_end": "2026-10-04T19:30:05.000+08:00",
        })
        cur = services.current_alignment(self.conn)
        entry = cur["segments"][0]
        self.assertEqual(entry["status"], "unlocatable")
        self.assertEqual(entry["reason"], "outside_anchor_coverage")


class TestPublishAndCorrection(ServiceTestBase):
    def test_correction_displaces_and_versions_immutable(self):
        services.register_device(self.conn, "DEV-A")
        services.add_anchors(self.conn, "DEV-A", [
            {"local_time": "2026-10-04T18:00:00.000+08:00",
             "utc_time": "2026-10-04T10:00:00.000Z"},
            {"local_time": "2026-10-04T19:00:00.000+08:00",
             "utc_time": "2026-10-04T10:59:59.000Z"},
        ])
        services.upsert_segments(self.conn, {
            "collection_id": "SEG-1", "device_id": "DEV-A",
            "local_start": "2026-10-04T18:30:00.000+08:00",
            "local_end": "2026-10-04T18:30:10.000+08:00",
        })
        v1 = services.publish_timeline(self.conn)
        self.assertEqual(v1["version"], 1)
        seg_v1 = v1["segments"][0]
        self.assertEqual(seg_v1["utc_start"], "2026-10-04T10:29:59.500Z")

        # 更正锚点：漂移消失
        services.replace_anchors(self.conn, "DEV-A", [
            {"local_time": "2026-10-04T18:00:00.000+08:00",
             "utc_time": "2026-10-04T10:00:00.000Z"},
            {"local_time": "2026-10-04T19:00:00.000+08:00",
             "utc_time": "2026-10-04T11:00:00.000Z"},
        ])
        v2 = services.publish_timeline(self.conn)
        self.assertEqual(v2["version"], 2)
        displaced = v2["changes"]["displaced"]
        self.assertEqual(len(displaced), 1)
        self.assertEqual(displaced[0]["shift_ms"]["start"], 500)
        # 结束点在 18:30:10，漂移按比例为 10000/3600000*1000 ≈ 3ms
        self.assertEqual(displaced[0]["shift_ms"]["end"], 503)

        # v1 不可变
        old = services.get_version(self.conn, 1)
        self.assertEqual(old["segments"][0]["utc_start"], "2026-10-04T10:29:59.500Z")
        self.assertEqual(len(old["anchors"][0]["anchors"]), 2)
        self.assertEqual(
            old["anchors"][0]["anchors"][1]["utc_time"],
            "2026-10-04T10:59:59.000Z",
        )

    def test_add_remove_in_changes(self):
        services.register_device(self.conn, "DEV-A")
        services.add_anchors(self.conn, "DEV-A", [
            {"local_time": "2026-10-04T10:00:00.000",
             "utc_time": "2026-10-04T10:00:00.000Z"},
            {"local_time": "2026-10-04T12:00:00.000",
             "utc_time": "2026-10-04T12:00:00.000Z"},
        ])
        services.upsert_segments(self.conn, [
            {"collection_id": "S1", "device_id": "DEV-A",
             "local_start": "2026-10-04T10:05:00.000",
             "local_end": "2026-10-04T10:05:10.000"},
            {"collection_id": "S2", "device_id": "DEV-A",
             "local_start": "2026-10-04T10:06:00.000",
             "local_end": "2026-10-04T10:06:10.000"},
        ])
        services.publish_timeline(self.conn)

        # 移除 S1（直接删表行模拟声段撤销），新增 S3
        self.conn.execute("DELETE FROM segments WHERE collection_id='S1'")
        services.upsert_segments(self.conn, {
            "collection_id": "S3", "device_id": "DEV-A",
            "local_start": "2026-10-04T10:07:00.000",
            "local_end": "2026-10-04T10:07:10.000",
        })
        v2 = services.publish_timeline(self.conn)
        changes = v2["changes"]
        self.assertEqual({e["collection_id"] for e in changes["added"]}, {"S3"})
        self.assertEqual({e["collection_id"] for e in changes["removed"]}, {"S1"})
        self.assertEqual(changes["displaced"], [])

    def test_stable_sorting(self):
        services.register_device(self.conn, "DEV-A")
        services.register_device(self.conn, "DEV-B")
        for dev in ("DEV-A", "DEV-B"):
            services.add_anchors(self.conn, dev, [
                {"local_time": "2026-10-04T10:00:00.000",
                 "utc_time": "2026-10-04T10:00:00.000Z"},
                {"local_time": "2026-10-04T12:00:00.000",
                 "utc_time": "2026-10-04T12:00:00.000Z"},
            ])
        services.upsert_segments(self.conn, [
            {"collection_id": "C", "device_id": "DEV-A",
             "local_start": "2026-10-04T10:10:00.000",
             "local_end": "2026-10-04T10:10:01.000"},
            {"collection_id": "A", "device_id": "DEV-A",
             "local_start": "2026-10-04T10:05:00.000",
             "local_end": "2026-10-04T10:05:01.000"},
            {"collection_id": "B", "device_id": "DEV-B",
             "local_start": "2026-10-04T10:05:00.000",
             "local_end": "2026-10-04T10:05:01.000"},
        ])
        v = services.publish_timeline(self.conn)
        order = [(s["utc_start"], s["device_id"], s["collection_id"])
                 for s in v["segments"]]
        self.assertEqual(order, [
            ("2026-10-04T10:05:00.000Z", "DEV-A", "A"),
            ("2026-10-04T10:05:00.000Z", "DEV-B", "B"),
            ("2026-10-04T10:10:00.000Z", "DEV-A", "C"),
        ])


class TestConcurrentPublish(unittest.TestCase):
    """更正与发布并发：每个版本必须自洽（版本内的锚点快照能推出全部声段）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "concurrent.db")
        dbmod.init_db(self.db_path)
        conn = dbmod.connect(self.db_path)
        try:
            services.register_device(conn, "DEV-A")
            services.add_anchors(conn, "DEV-A", [
                {"local_time": "2026-10-04T18:00:00.000+08:00",
                 "utc_time": "2026-10-04T10:00:00.000Z"},
                {"local_time": "2026-10-04T19:00:00.000+08:00",
                 "utc_time": "2026-10-04T10:59:59.000Z"},
            ])
            services.upsert_segments(conn, {
                "collection_id": "SEG-1", "device_id": "DEV-A",
                "local_start": "2026-10-04T18:30:00.000+08:00",
                "local_end": "2026-10-04T18:30:10.000+08:00",
            })
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish_vs_correction(self):
        errors: list[Exception] = []
        published: list[int] = []

        def publisher():
            try:
                for _ in range(20):
                    conn = dbmod.connect(self.db_path)
                    try:
                        v = services.publish_timeline(conn)
                        published.append(v["version"])
                    finally:
                        conn.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        corrector_anchors = [
            [
                {"local_time": "2026-10-04T18:00:00.000+08:00",
                 "utc_time": "2026-10-04T10:00:00.000Z"},
                {"local_time": "2026-10-04T19:00:00.000+08:00",
                 "utc_time": "2026-10-04T10:59:59.000Z"},
            ],
            [
                {"local_time": "2026-10-04T18:00:00.000+08:00",
                 "utc_time": "2026-10-04T10:00:00.000Z"},
                {"local_time": "2026-10-04T19:00:00.000+08:00",
                 "utc_time": "2026-10-04T11:00:00.000Z"},
            ],
        ]

        def corrector():
            try:
                for i in range(20):
                    conn = dbmod.connect(self.db_path)
                    try:
                        services.replace_anchors(conn, "DEV-A", corrector_anchors[i % 2])
                    finally:
                        conn.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=publisher)
        t2 = threading.Thread(target=corrector)
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)
        self.assertEqual(errors, [])

        # 每个版本都必须自洽：SEG-1 的 UTC 起点只能是 10:29:59.500（旧锚点）
        # 或 10:30:00.000（新锚点），且与该版本自己保存的锚点快照一致
        conn = dbmod.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT version, entries_json, anchors_json "
                "FROM timeline_versions ORDER BY version"
            ).fetchall()
            self.assertGreaterEqual(len(rows), 2)
            for r in rows:
                import json
                entries = json.loads(r["entries_json"])["entries"]
                anchors = json.loads(r["anchors_json"])[0]["anchors"]
                seg = next(e for e in entries if e["collection_id"] == "SEG-1")
                last_anchor_utc = anchors[-1]["utc_time"]
                if last_anchor_utc == "2026-10-04T10:59:59.000Z":
                    self.assertEqual(seg["utc_start"],
                                     _utc_ms("2026-10-04T10:29:59.500Z"))
                else:
                    self.assertEqual(seg["utc_start"],
                                     _utc_ms("2026-10-04T10:30:00.000Z"))
        finally:
            conn.close()


def _utc_ms(text: str) -> int:
    return dt_to_ms(parse_time(text, require_tz=True))


if __name__ == "__main__":
    unittest.main()
