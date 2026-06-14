FROM python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0

WORKDIR /app

COPY services/telemetry-gateway/requirements.txt ./requirements.txt
RUN pip install --no-index --find-links=/wheels -r requirements.txt || \
    pip install --no-cache-dir -r requirements.txt

COPY services/telemetry-gateway/app.py ./app.py

ENV PYTHONUNBUFFERED="1"

HEALTHCHECK --interval=10s --timeout=5s --retries=6 --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9200/health', timeout=4)"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9200", "--workers", "1"]
