#!/usr/bin/env bash
# 端到端调用示例：注册设备 -> 录锚点 -> 提交声段 -> 发布 -> 锚点更正 -> 再发布
# 依赖：bash、curl；默认服务地址 http://127.0.0.1:8080
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
j() { python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))'; }

echo "== 1. 注册两台设备 =="
curl -sS -X POST "$BASE_URL/api/devices" \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"DEV-A","name":"现场录音机 A"}' | j
curl -sS -X POST "$BASE_URL/api/devices" \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"DEV-B","name":"现场录音机 B"}' | j

echo "== 2. 录入锚点（设备 A：本地比 UTC 快 8 小时，时钟略快 -> 漂移）=="
curl -sS -X POST "$BASE_URL/api/devices/DEV-A/anchors" \
  -H 'Content-Type: application/json' \
  -d '[
    {"local_time":"2026-10-04T18:00:00.000+08:00","utc_time":"2026-10-04T10:00:00.000Z"},
    {"local_time":"2026-10-04T19:00:00.000+08:00","utc_time":"2026-10-04T10:59:59.000Z"}
  ]' | j

echo "== 3. 设备 B：naive 本地时间（视作设备墙上钟），无漂移 =="
curl -sS -X POST "$BASE_URL/api/devices/DEV-B/anchors" \
  -H 'Content-Type: application/json' \
  -d '[
    {"local_time":"2026-10-04T10:00:00.000","utc_time":"2026-10-04T10:00:00.000Z"},
    {"local_time":"2026-10-04T11:00:00.000","utc_time":"2026-10-04T11:00:00.000Z"}
  ]' | j

echo "== 4. 批量提交声段（SEG-3 落在锚点覆盖范围外，将无法定位）=="
curl -sS -X POST "$BASE_URL/api/segments" \
  -H 'Content-Type: application/json' \
  -d '[
    {"collection_id":"SEG-1","device_id":"DEV-A",
     "local_start":"2026-10-04T18:30:00.000+08:00","local_end":"2026-10-04T18:30:10.000+08:00"},
    {"collection_id":"SEG-2","device_id":"DEV-B",
     "local_start":"2026-10-04T10:20:00.000","local_end":"2026-10-04T10:20:05.000"},
    {"collection_id":"SEG-3","device_id":"DEV-A",
     "local_start":"2026-10-04T20:00:00.000+08:00","local_end":"2026-10-04T20:00:03.000+08:00"}
  ]' | j

echo "== 5. 同内容重传（幂等，inserted=0, idempotent=1）=="
curl -sS -X POST "$BASE_URL/api/segments" \
  -H 'Content-Type: application/json' \
  -d '{"collection_id":"SEG-1","device_id":"DEV-A",
       "local_start":"2026-10-04T18:30:00.000+08:00","local_end":"2026-10-04T18:30:10.000+08:00"}' | j

echo "== 6. 查看当前草稿时间轴 =="
curl -sS "$BASE_URL/api/timeline/current" | j

echo "== 7. 发布 v1 =="
curl -sS -X POST "$BASE_URL/api/timeline/publish" | j

echo "== 8. 锚点更正（设备 A 时钟修正：19:00 本地对应 11:00:00 UTC）=="
curl -sS -X PUT "$BASE_URL/api/devices/DEV-A/anchors" \
  -H 'Content-Type: application/json' \
  -d '[
    {"local_time":"2026-10-04T18:00:00.000+08:00","utc_time":"2026-10-04T10:00:00.000Z"},
    {"local_time":"2026-10-04T19:00:00.000+08:00","utc_time":"2026-10-04T11:00:00.000Z"}
  ]' | j

echo "== 9. 再发布 v2（SEG-1 发生位移；v1 保持不变）=="
curl -sS -X POST "$BASE_URL/api/timeline/publish" | j

echo "== 10. 版本列表与 v1 快照（内容不变）=="
curl -sS "$BASE_URL/api/timeline/versions" | j
curl -sS "$BASE_URL/api/timeline/versions/1" | j

echo
echo "== 附：异常演示 =="
echo "-- 声段起止倒置（422 拒绝）"
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/api/segments" \
  -H 'Content-Type: application/json' \
  -d '{"collection_id":"SEG-BAD","device_id":"DEV-A",
       "local_start":"2026-10-04T18:30:10.000+08:00","local_end":"2026-10-04T18:30:00.000+08:00"}'
echo "-- 异内容重传冲突（409）"
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/api/segments" \
  -H 'Content-Type: application/json' \
  -d '{"collection_id":"SEG-1","device_id":"DEV-A",
       "local_start":"2026-10-04T18:30:01.000+08:00","local_end":"2026-10-04T18:30:10.000+08:00"}'
echo "-- 未知设备（404，不写入）"
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/api/segments" \
  -H 'Content-Type: application/json' \
  -d '{"collection_id":"SEG-X","device_id":"DEV-UNKNOWN",
       "local_start":"2026-10-04T18:30:00.000+08:00","local_end":"2026-10-04T18:30:05.000+08:00"}'
