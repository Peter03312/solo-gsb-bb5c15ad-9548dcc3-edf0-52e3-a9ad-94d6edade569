# 多设备现场录音时间轴对齐服务

多台录音设备各自走时（时钟速率与偏移）不同。本服务录入每台设备的
**同步锚点**（带毫秒的设备本地时间 ↔ UTC 时间），再提交带唯一采集编号的
**声段**（设备本地起止时间），即可：

- 在相邻锚点之间按**线性漂移**把声段起止时间映射为 UTC；
- 起止任一时刻落在锚点覆盖范围外的声段标记为 `unlocatable`（无法定位）；
- 锚点可随时**更正**并重算，已发布的时间轴**版本永久不可变**；
- 每次发布生成新版本，并列出相对上一版的**位移 / 新增 / 移除**声段；
- 声段按 **UTC 起点 → 设备编号 → 采集编号** 稳定排序。

技术栈：Python 3.12 · FastAPI · SQLite（WAL 模式，单写入者事务）。

---

## 1. 启动

### 方式一：Docker Compose（推荐，一键启动并初始化数据库）

```bash
docker compose up --build -d
```

- 服务地址：`http://localhost:8080`
- 数据库：容器内 `/data/alignment.db`，通过命名卷 `alignment-data` 持久化；
  容器启动时自动幂等建表（无需手工初始化）。
- 健康检查：`GET http://localhost:8080/healthz`

停止：

```bash
docker compose down            # 保留数据卷
docker compose down -v         # 同时删除数据库卷
```

### 方式二：本地直接运行

```bash
python3 -m pip install -r requirements.txt
# 数据库文件路径（父目录会自动创建）；默认 /data/alignment.db
export ALIGNMENT_DB_PATH=./data/alignment.db
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

启动时同样会自动建表初始化数据库。

---

## 2. 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ALIGNMENT_DB_PATH` | `/data/alignment.db` | SQLite 数据库文件路径，父目录自动创建 |

服务监听地址与端口由 uvicorn 参数控制（Dockerfile / Compose 中固定为
`0.0.0.0:8080`）：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

时间格式约定（ISO 8601，毫秒精度）：

- `utc_time` 必须带时区，建议以 `Z` 结尾，如 `2026-10-04T10:00:00.000Z`；
- 设备 `local_time` 可带固定偏移（如 `+08:00`），也可不带（naive，视为设备墙上钟）；
  同一设备的所有锚点必须使用统一形态与相同偏移；
- 不接受亚毫秒精度（如 `.0001`），不静默截断；
- 声段起止时间必须同为 aware 或同为 naive 且偏移相同，且 `start < end`。

---

## 3. HTTP API

交互式文档（Swagger UI）：`http://localhost:8080/docs`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/devices` | 注册设备（重复注册幂等） |
| POST | `/api/devices/{device_id}/anchors` | 增量录入锚点（单个对象或数组） |
| PUT | `/api/devices/{device_id}/anchors` | **锚点更正**：用完整锚点集替换该设备全部锚点 |
| POST | `/api/segments` | 提交声段（单个对象或数组，整批原子） |
| GET | `/api/timeline/current?device_id=` | 当前草稿时间轴（用最新锚点即时重算） |
| POST | `/api/timeline/publish` | 发布不可变新版本，返回与上一版差异 |
| GET | `/api/timeline/versions` | 版本列表 |
| GET | `/api/timeline/versions/{n}` | 第 n 版完整快照（声段 + 当时的锚点） |
| GET | `/healthz` | 健康检查 |

### 3.1 注册设备

```bash
curl -X POST http://localhost:8080/api/devices \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"DEV-A","name":"现场录音机 A"}'
```

### 3.2 录入同步锚点

```bash
curl -X POST http://localhost:8080/api/devices/DEV-A/anchors \
  -H 'Content-Type: application/json' \
  -d '[
    {"local_time":"2026-10-04T18:00:00.000+08:00","utc_time":"2026-10-04T10:00:00.000Z"},
    {"local_time":"2026-10-04T19:00:00.000+08:00","utc_time":"2026-10-04T10:59:59.000Z"}
  ]'
```

校验规则（违反则拒绝，整批不写入）：本地时间必须严格递增；按本地时间排序后
UTC 时间也必须严格递增；不允许同一本地时刻重复锚点。

### 3.3 提交声段

```bash
curl -X POST http://localhost:8080/api/segments \
  -H 'Content-Type: application/json' \
  -d '{
    "collection_id":"SEG-1",
    "device_id":"DEV-A",
    "local_start":"2026-10-04T18:30:00.000+08:00",
    "local_end":"2026-10-04T18:30:10.000+08:00"
  }'
```

- **幂等**：相同 `collection_id` 且设备与起止时间内容完全一致的重传，返回
  `inserted=0, idempotent=1`；
- **冲突**：相同 `collection_id` 但内容不同，返回 `409 SEGMENT_CONTENT_CONFLICT`，
  整批拒绝写入；
- 声段起止倒置返回 `422 SEGMENT_RANGE_INVALID`；未知设备返回 `404 UNKNOWN_DEVICE`
  且不写入任何数据。

### 3.4 锚点更正、发布与版本差异

```bash
# 更正：提交该设备的完整锚点集（旧锚点被原子替换）
curl -X PUT http://localhost:8080/api/devices/DEV-A/anchors \
  -H 'Content-Type: application/json' \
  -d '[
    {"local_time":"2026-10-04T18:00:00.000+08:00","utc_time":"2026-10-04T10:00:00.000Z"},
    {"local_time":"2026-10-04T19:00:00.000+08:00","utc_time":"2026-10-04T11:00:00.000Z"}
  ]'

# 发布新版本：changes.displaced 中给出位移声段的 previous 与 shift_ms
curl -X POST http://localhost:8080/api/timeline/publish
```

`changes` 结构：

```json
{
  "added":     [ { "...声段完整条目" } ],
  "removed":   [ { "...声段完整条目" } ],
  "displaced": [ {
    "...声段完整条目": "",
    "previous": {"status": "locatable", "utc_start": "...", "utc_end": "..."},
    "shift_ms": {"start": 500, "end": 503}
  } ]
}
```

已发布版本通过 `GET /api/timeline/versions/{n}` 取回，内容（含当时完整锚点快照）
永不随后续更正而改变。

### 声段条目

```json
{
  "collection_id": "SEG-1",
  "device_id": "DEV-A",
  "local_start": "2026-10-04T18:30:00.000+08:00",
  "local_end": "2026-10-04T18:30:10.000+08:00",
  "status": "locatable",
  "utc_start": "2026-10-04T10:29:59.500Z",
  "utc_end": "2026-10-04T10:30:09.497Z"
}
```

无法定位时：

```json
{
  "status": "unlocatable",
  "utc_start": null,
  "utc_end": null,
  "reason": "outside_anchor_coverage"
}
```

`reason` 另可能为 `timezone_mismatch`（声段时区形态与设备锚点不一致）。

### 错误响应统一格式

```json
{"error": {"code": "SEGMENT_RANGE_INVALID", "message": "……", "details": {}}}
```

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 400 | `INVALID_TIME` / `INVALID_ANCHOR` / `INVALID_SEGMENT` / `INVALID_JSON` | 非法时间/请求体 |
| 404 | `UNKNOWN_DEVICE` / `VERSION_NOT_FOUND` | 未知设备、版本不存在 |
| 409 | `ANCHOR_CONFLICT` / `SEGMENT_CONTENT_CONFLICT` | 重复锚点、异内容重传 |
| 422 | `ANCHOR_NOT_STRICTLY_INCREASING` / `SEGMENT_RANGE_INVALID` | 锚点倒挂、声段起止倒置 |

---

## 4. 映射与一致性模型

- 每台设备的锚点按本地时间排序；声段在相邻锚点构成的半开区间
  `[a_local, b_local)` 内做整数毫秒线性插值（half-up 四舍五入）：

  `utc(t) = utc_a + (t − local_a) · (utc_b − utc_a) / (local_b − local_a)`

  早于首个锚点或晚于末个锚点的任一端点 → `unlocatable`。
  锚点可在声段之后补录：补录前草稿中该声段为 `unlocatable`，
  补录（或更正）后在草稿时间轴即时重算为 `locatable`。
- 所有写操作使用 `BEGIN IMMEDIATE` 事务 + WAL；「锚点更正」与「时间轴发布」
  并发时写操作序列化，**每个版本对应同一时刻的一套完整锚点与声段快照**，
  快照以 JSON 存入 `timeline_versions`，与当前工作表解耦。
- 更正锚点只影响草稿时间轴（`/current`）和之后发布的新版本；历史版本不可变。

---

## 5. 调用示例

```bash
# 端到端：注册 → 锚点 → 声段 → 发布 → 更正 → 再发布 → 异常演示
BASE_URL=http://localhost:8080 bash examples/demo.sh

# 仅标准库的 Python 客户端
BASE_URL=http://localhost:8080 python3 examples/client_example.py
```

---

## 6. 运行测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：漂移插值（中点/端点/越界/正负漂移舍入）、锚点严格递增与偏移一致性、
声段倒置、未知设备不写入、幂等与冲突、锚点更正后的位移差异、历史版本不可变、
稳定排序，以及更正/发布多线程并发下的版本自洽性。
