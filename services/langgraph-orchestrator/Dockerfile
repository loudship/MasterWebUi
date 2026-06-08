FROM python:3.11-slim

WORKDIR /app/orchestrator

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY services/langgraph-orchestrator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/langgraph_orchestrator.py backend/hitl_broker.py ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QDRANT_URI=http://qdrant:6333 \
    REDIS_URL=redis://redis-cache:6379/0 \
    LM_STUDIO_BASE_URL=http://host.docker.internal:4321

RUN addgroup --system orchestrator \
 && adduser --system --ingroup orchestrator orchestrator
USER orchestrator

EXPOSE 8100

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD curl -fsS http://127.0.0.1:8100/health >/dev/null || exit 1

CMD ["python", "-m", "uvicorn", "langgraph_orchestrator:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1", "--log-level", "info"]
