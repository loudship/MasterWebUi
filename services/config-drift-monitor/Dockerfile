FROM python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0

WORKDIR /app
COPY services/config-drift-monitor/requirements.txt ./requirements.txt
COPY wheelhouse/langgraph-orchestrator /wheelhouse
RUN pip install --no-cache-dir --no-index --find-links=/wheelhouse -r requirements.txt \
 && rm -rf /wheelhouse

COPY services/config-drift-monitor/app.py ./app.py
COPY services/config-drift-monitor/config_drift ./config_drift
COPY services/config-drift-monitor/ui ./ui

ENV OPEN_WEBUI_URL="http://open-webui:8080" \
    CONFIG_DRIFT_BASELINE_PATH="/app/config/config-drift-baseline.yaml" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1"

RUN addgroup --system driftmonitor \
 && adduser --system --ingroup driftmonitor driftmonitor
USER driftmonitor

EXPOSE 9100
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9100/api/v1/health', timeout=4)"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9100", "--workers", "1"]

