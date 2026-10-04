FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ALIGNMENT_DB_PATH=/data/alignment.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY examples ./examples

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# 容器启动时 uvicorn 的 lifespan 会幂等建表，完成数据库初始化
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
