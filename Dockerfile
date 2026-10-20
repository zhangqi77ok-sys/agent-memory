FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir . && useradd -r app
USER app
EXPOSE 8000
# ROLE=api|worker；worker 在 v1 后续提交实现
CMD ["sh", "-c", "if [ \"$ROLE\" = worker ]; then python -m app.worker; else uvicorn app.main:app --host 0.0.0.0 --port $PORT; fi"]
# worker 没有 HTTP 端口，健康检查直接放行；api 探 $PORT
HEALTHCHECK --interval=30s --timeout=3s CMD [ "$ROLE" = worker ] || python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz')" || exit 1
