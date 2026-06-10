FROM quay.io/docling-project/docling-serve@sha256:be409c69590e9346ba1b7f1827b02248ccb47629f2e6de2a641171e76ae84fff

WORKDIR /opt/app-root/src
COPY services/docling-ui/docling_ui_app.py ./docling_ui_app.py

ENV DOCLING_SERVE_ENABLE_UI="true" \
    GRADIO_ANALYTICS_ENABLED="false" \
    UVICORN_WORKERS="1"

CMD ["python", "-m", "uvicorn", "docling_ui_app:app", \
     "--host", "0.0.0.0", "--port", "5001", "--workers", "1"]
