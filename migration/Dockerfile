FROM open-webui:0.9.6@sha256:2ee1870d76187136f5f8620ecb7d13b36113098ad8b209852ab66d089486ca20

WORKDIR /app/backend

COPY migration/migrate_state.py /app/migration/migrate_state.py
COPY scripts/qdrant_alias_cutover.py /app/migration/qdrant_alias_cutover.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENABLE_DB_MIGRATIONS=true \
    ENABLE_PERSISTENT_CONFIG=false

ENTRYPOINT ["python", "/app/migration/migrate_state.py"]
