"""FastAPI 入口：设备、锚点、声段、时间轴版本等 HTTP 接口。

端点均为同步 def：FastAPI 会把它们放到线程池执行，SQLite 的阻塞调用不会
卡住事件循环，从而让「锚点更正」与「时间轴发布」在 HTTP 层也能真正并发
（数据库侧再由 BEGIN IMMEDIATE 序列化，保证版本快照一致）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional, Union

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import db as dbmod
from . import services


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器启动即幂等建表初始化数据库
    dbmod.init_db()
    yield


app = FastAPI(
    title="多设备现场录音时间轴对齐服务",
    version="1.0.0",
    description="录入同步锚点与声段，按设备线性漂移映射 UTC，发布不可变时间轴版本。",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 请求模型

class DeviceIn(BaseModel):
    device_id: str = Field(..., description="设备编号（唯一）", examples=["DEV-A"])
    name: Optional[str] = Field(None, description="设备备注名")


class AnchorIn(BaseModel):
    local_time: str = Field(
        ..., description="设备本地时间，毫秒精度；可带固定偏移或不带时区",
        examples=["2026-10-04T18:00:00.000+08:00"])
    utc_time: str = Field(
        ..., description="对应的 UTC 时间，毫秒精度，必须带时区（Z）",
        examples=["2026-10-04T10:00:00.000Z"])


class SegmentIn(BaseModel):
    collection_id: str = Field(..., description="唯一采集编号", examples=["SEG-1"])
    device_id: str = Field(..., description="采集设备编号（须已注册）")
    local_start: str = Field(..., description="声段本地起点，毫秒精度")
    local_end: str = Field(..., description="声段本地终点，毫秒精度，须严格晚于起点")


@app.exception_handler(services.ApiError)
async def api_error_handler(request: Request, exc: services.ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message,
                           "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """JSON 解析失败、缺字段、类型错误等统一转成 400 INVALID_REQUEST。"""
    return JSONResponse(
        status_code=400,
        content={"error": {
            "code": "INVALID_REQUEST",
            "message": "请求体不合法（JSON 语法错误或字段缺失/类型错误）",
            "details": {"errors": exc.errors()}},
        },
    )


@app.get("/healthz")
def healthz():
    conn = dbmod.connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}


# ---------------------------------------------------------------- 设备

@app.post("/api/devices", status_code=201)
def create_device(body: DeviceIn):
    conn = dbmod.connect()
    try:
        return services.register_device(conn, body.device_id, body.name)
    finally:
        conn.close()


# ---------------------------------------------------------------- 锚点

@app.post("/api/devices/{device_id}/anchors")
def append_anchors(device_id: str,
                   payload: Union[AnchorIn, list[AnchorIn]] = Body(...)):
    """增量录入一个或一批同步锚点（严格递增，重复/倒挂拒绝写入）。"""
    body = payload if isinstance(payload, list) else [payload]
    conn = dbmod.connect()
    try:
        return services.add_anchors(conn, device_id, [a.model_dump() for a in body])
    finally:
        conn.close()


@app.put("/api/devices/{device_id}/anchors")
def correct_anchors(device_id: str,
                    payload: Union[AnchorIn, list[AnchorIn]] = Body(...)):
    """锚点更正：用完整锚点集替换该设备锚点；已发布版本快照不变。"""
    body = payload if isinstance(payload, list) else [payload]
    conn = dbmod.connect()
    try:
        return services.replace_anchors(conn, device_id, [a.model_dump() for a in body])
    finally:
        conn.close()


# ---------------------------------------------------------------- 声段

@app.post("/api/segments")
def submit_segments(payload: Union[SegmentIn, list[SegmentIn]] = Body(...)):
    """提交声段，支持单个对象或数组；同内容重传幂等，异内容 409。"""
    body = payload if isinstance(payload, list) else [payload]
    conn = dbmod.connect()
    try:
        return services.upsert_segments(conn, [s.model_dump() for s in body])
    finally:
        conn.close()


# ---------------------------------------------------------------- 时间轴

@app.get("/api/timeline/current")
def get_current_timeline(device_id: Optional[str] = None):
    """当前草稿时间轴（锚点更正后立即体现，尚未发布）。"""
    conn = dbmod.connect()
    try:
        return services.current_alignment(conn, device_id)
    finally:
        conn.close()


@app.post("/api/timeline/publish")
def publish():
    """发布不可变新版本，返回与上一版的新增 / 移除 / 位移差异。"""
    conn = dbmod.connect()
    try:
        return services.publish_timeline(conn)
    finally:
        conn.close()


@app.get("/api/timeline/versions")
def versions():
    conn = dbmod.connect()
    try:
        return services.list_versions(conn)
    finally:
        conn.close()


@app.get("/api/timeline/versions/{version}")
def version_detail(version: int):
    conn = dbmod.connect()
    try:
        return services.get_version(conn, version)
    finally:
        conn.close()
