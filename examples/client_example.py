#!/usr/bin/env python3
"""调用示例（仅依赖标准库）：注册设备、录锚点、提交声段、发布、锚点更正。

用法：
    BASE_URL=http://127.0.0.1:8080 python3 examples/client_example.py
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")


def call(method: str, path: str, payload: object | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise SystemExit(f"HTTP {exc.code} {method} {path}\n{body}") from exc


def main() -> None:
    # 1. 注册设备
    call("POST", "/api/devices", {"device_id": "DEV-A", "name": "现场录音机 A"})

    # 2. 录入锚点（本地 +08:00，首小时漂移 -1 秒）
    call("POST", "/api/devices/DEV-A/anchors", [
        {"local_time": "2026-10-04T18:00:00.000+08:00",
         "utc_time": "2026-10-04T10:00:00.000Z"},
        {"local_time": "2026-10-04T19:00:00.000+08:00",
         "utc_time": "2026-10-04T10:59:59.000Z"},
    ])

    # 3. 提交声段
    call("POST", "/api/segments", {
        "collection_id": "SEG-1",
        "device_id": "DEV-A",
        "local_start": "2026-10-04T18:30:00.000+08:00",
        "local_end": "2026-10-04T18:30:10.000+08:00",
    })

    # 4. 同内容重传 -> 幂等
    retried = call("POST", "/api/segments", {
        "collection_id": "SEG-1",
        "device_id": "DEV-A",
        "local_start": "2026-10-04T18:30:00.000+08:00",
        "local_end": "2026-10-04T18:30:10.000+08:00",
    })
    assert retried["inserted"] == 0 and retried["idempotent"] == 1, retried

    # 5. 发布 v1
    v1 = call("POST", "/api/timeline/publish")
    print("v1 SEG-1:", next(s for s in v1["segments"] if s["collection_id"] == "SEG-1"))

    # 6. 锚点更正
    call("PUT", "/api/devices/DEV-A/anchors", [
        {"local_time": "2026-10-04T18:00:00.000+08:00",
         "utc_time": "2026-10-04T10:00:00.000Z"},
        {"local_time": "2026-10-04T19:00:00.000+08:00",
         "utc_time": "2026-10-04T11:00:00.000Z"},
    ])

    # 7. 发布 v2，查看位移（SEG-1 整体晚了 500ms）
    v2 = call("POST", "/api/timeline/publish")
    print("v2 changes:", json.dumps(v2["changes"], ensure_ascii=False, indent=2))

    # 8. v1 是不可变快照
    v1_again = call("GET", "/api/timeline/versions/1")
    print("v1 immutable:", v1_again["segments"][0])


if __name__ == "__main__":
    main()
