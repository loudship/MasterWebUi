FROM open-webui-master-deep-web-mcp@sha256:7912a186a811339a946cad4fe3af2f2acb77adebe00e4951086dbf8f27d239db

WORKDIR /app

COPY deep-web-mcp/requirements.offline.txt ./requirements.offline.txt
COPY wheelhouse/deep-web-mcp /wheelhouse
RUN pip install --no-cache-dir --no-index --find-links=/wheelhouse -r requirements.offline.txt \
 && rm -rf /wheelhouse

COPY deep-web-mcp/ ./

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
