FROM python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0

WORKDIR /app/orchestrator

COPY services/langgraph-orchestrator/requirements.txt ./requirements.txt
COPY wheelhouse/langgraph-orchestrator /wheelhouse
RUN pip install --no-cache-dir --no-index --find-links=/wheelhouse -r requirements.txt \
 && rm -rf /wheelhouse

COPY services/langgraph-orchestrator/langgraph_orchestrator.py services/langgraph-orchestrator/hitl_broker.py ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QDRANT_URI=http://qdrant:6333 \
    QDRANT_NARRATIVE_ALIAS=narrative_active \
    REDIS_URL=redis://redis-hitl:6379/0 \
    INFERENCE_GATEWAY_URL=http://inference-gateway:4321 \
    LANGGRAPH_STRICT_MSGPACK=true

RUN addgroup --system orchestrator \
 && adduser --system --ingroup orchestrator orchestrator
USER orchestrator

EXPOSE 8100

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4)"

CMD ["python", "-m", "uvicorn", "langgraph_orchestrator:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1", "--log-level", "info"]
