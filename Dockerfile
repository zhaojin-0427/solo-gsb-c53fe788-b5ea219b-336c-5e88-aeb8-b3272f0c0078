FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用层缓存）
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 复制后端与前端
COPY backend /app/backend
COPY frontend /app/frontend

ENV KNIT_FRONTEND_DIR=/app/frontend \
    KNIT_DB_PATH=/data/knit.db \
    PYTHONPATH=/app/backend

VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--app-dir", "/app/backend", "--host", "0.0.0.0", "--port", "8000"]
