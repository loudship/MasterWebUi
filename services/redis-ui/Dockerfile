FROM python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0

WORKDIR /app
COPY services/redis-ui/requirements.txt ./requirements.txt
COPY wheelhouse/langgraph-orchestrator /wheelhouse
RUN pip install --no-cache-dir --no-index --find-links=/wheelhouse -r requirements.txt \
 && rm -rf /wheelhouse

COPY services/redis-ui/redis_ui.py ./redis_ui.py
COPY services/redis-ui/redis_ui.html ./redis_ui.html
COPY services/redis-ui/redis_console ./redis_console
COPY services/redis-ui/redis_ui ./redis_ui

ENV REDIS_CACHE_URL="redis://redis-cache:6379/0" \
    REDIS_UI_DB_PATH="/app/data/redis-console.db" \
    REDIS_UI_REPORT_RETENTION_DAYS="30" \
    REDIS_UI_REPORT_MAX_COUNT="200" \
    REDIS_UI_OVERVIEW_CACHE_TTL_S="10" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1"

RUN addgroup --system redisui \
 && adduser --system --ingroup redisui redisui \
 && mkdir -p /app/data \
 && chown -R redisui:redisui /app/data
USER redisui

EXPOSE 5540
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5540/api/health', timeout=4)"

CMD ["python", "-m", "uvicorn", "redis_ui:app", "--host", "0.0.0.0", "--port", "5540", "--workers", "1"]
