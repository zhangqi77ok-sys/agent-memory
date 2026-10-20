FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir . && useradd -r app
USER app
EXPOSE 8000
# ROLE=api|worker；worker 在 v1 后续提交实现
CMD ["sh", "-c", "if [ \"$ROLE\" = worker ]; then python -m app.worker; else uvicorn app.main:app --host 0.0.0.0 --port 8000; fi"]
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
