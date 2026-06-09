FROM python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0

WORKDIR /app/gateway

COPY services/inference-gateway/requirements.txt ./requirements.txt
COPY wheelhouse/inference-gateway /wheelhouse
RUN pip install --no-cache-dir --no-index --find-links=/wheelhouse -r requirements.txt \
 && rm -rf /wheelhouse

COPY services/inference-gateway/inference_gateway.py ./inference_gateway.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=4321

RUN addgroup --system gateway \
 && adduser --system --ingroup gateway gateway
USER gateway

EXPOSE 4321

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4321/health', timeout=4)"

CMD ["python", "-m", "uvicorn", "inference_gateway:app", "--host", "0.0.0.0", "--port", "4321", "--workers", "1", "--log-level", "info"]
