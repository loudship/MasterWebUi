"""Idempotent maintenance-window migration into PostgreSQL and aliased Qdrant."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Iterable

from qdrant_client import QdrantClient, models
from sqlalchemy import MetaData, create_engine, func, select, DateTime, String, BigInteger, Integer, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration")

OPEN_WEBUI_SQLITE_URL = os.environ.get(
    "OPEN_WEBUI_SQLITE_URL", "sqlite:////migration-data/open-webui/webui.db?mode=ro"
)
OPEN_WEBUI_POSTGRES_URL = os.environ["OPEN_WEBUI_POSTGRES_URL"]
DEEP_WEB_SQLITE_URL = os.environ.get(
    "DEEP_WEB_SQLITE_URL", "sqlite:////migration-data/deep-web-mcp/auth_vault.db?mode=ro"
)
DEEP_WEB_POSTGRES_URL = os.environ["DEEP_WEB_POSTGRES_URL"]
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
CHROMA_PATH = Path(os.environ.get("CHROMA_PATH", "/migration-data/open-webui/vector_db"))
NARRATIVE_ALIAS = os.environ.get("QDRANT_NARRATIVE_ALIAS", "narrative_active")
NARRATIVE_COLLECTION = os.environ.get(
    "QDRANT_NARRATIVE_BOOTSTRAP_COLLECTION", "primary_narrative"
)
BATCH_SIZE = 500


def _initialize_open_webui_schema() -> None:
    import sys
    if "/app/backend" not in sys.path:
        sys.path.insert(0, "/app/backend")
    os.environ["DATABASE_URL"] = OPEN_WEBUI_POSTGRES_URL
    os.environ["ENABLE_DB_MIGRATIONS"] = "true"
    import open_webui.config  # noqa: F401


def _sanitize_value(val, target_type):
    if val is None or val == "":
        return None
        
    is_datetime = isinstance(target_type, DateTime)
    is_integer = isinstance(target_type, (Integer, BigInteger))
    
    if is_datetime:
        if isinstance(val, (int, float)):
            if val > 1e11:
                return datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
            return datetime.fromtimestamp(val, tz=timezone.utc)
        if isinstance(val, str):
            val = val.strip()
            if not val:
                return None
            if val.replace(".", "", 1).isdigit():
                dval = float(val)
                if dval > 1e11:
                    return datetime.fromtimestamp(dval / 1000.0, tz=timezone.utc)
                return datetime.fromtimestamp(dval, tz=timezone.utc)
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except Exception:
                for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d"):
                    try:
                        return datetime.strptime(val, fmt)
                    except Exception:
                        continue
                logger.warning("[MIGRATION] Could not parse datetime string: %r", val)
                return None
        return val
        
    elif is_integer:
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            val = val.strip()
            if not val:
                return None
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return int(dt.timestamp())
            except Exception:
                for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(val, fmt)
                        return int(dt.timestamp())
                    except Exception:
                        continue
            if val.replace(".", "", 1).isdigit():
                return int(float(val))
        return val
        
    return val


def _copy_relational(source_url: str, target_url: str, label: str) -> dict[str, int]:
    source = create_engine(source_url)
    target = create_engine(target_url, pool_pre_ping=True)
    source_metadata = MetaData()
    target_metadata = MetaData()
    source_metadata.reflect(bind=source)
    target_metadata.reflect(bind=target)
    counts: dict[str, int] = {}

    for source_table in source_metadata.sorted_tables:
        if source_table.name not in target_metadata.tables:
            logger.info("[MIGRATION] Skipping legacy table: %s", source_table.name)
            continue
        target_table = target_metadata.tables[source_table.name]
        primary_keys = [column.name for column in target_table.primary_key.columns]
        copied = 0

        # Override reflected DateTime columns to String to prevent SQLite driver conversion errors
        orig_datetime_cols = {}
        for column in source_table.columns:
            if isinstance(column.type, DateTime):
                target_col = target_table.columns.get(column.name)
                target_type = target_col.type if target_col is not None else DateTime()
                orig_datetime_cols[column.name] = target_type
                column.type = String()

        with source.connect() as source_connection, target.begin() as target_connection:
            target_connection.execute(text("SET session_replication_role = 'replica';"))
            result = source_connection.execute(select(source_table))
            while True:
                rows = result.mappings().fetchmany(BATCH_SIZE)
                if not rows:
                    break
                
                values = []
                for row in rows:
                    row_dict = dict(row)
                    for col_name, target_type in orig_datetime_cols.items():
                        if col_name in row_dict:
                            row_dict[col_name] = _sanitize_value(row_dict[col_name], target_type)
                    values.append(row_dict)
                
                statement = pg_insert(target_table).values(values)
                if primary_keys:
                    update_values = {
                        column.name: statement.excluded[column.name]
                        for column in target_table.columns
                        if column.name not in primary_keys
                    }
                    statement = (
                        statement.on_conflict_do_update(
                            index_elements=primary_keys,
                            set_=update_values,
                        )
                        if update_values
                        else statement.on_conflict_do_nothing(index_elements=primary_keys)
                    )
                target_connection.execute(statement)
                copied += len(values)

        with source.connect() as source_connection, target.connect() as target_connection:
            source_count = source_connection.scalar(select(func.count()).select_from(source_table))
            target_count = target_connection.scalar(select(func.count()).select_from(target_table))
        if target_count < source_count:
            raise RuntimeError(
                f"{label}.{source_table.name}: target count {target_count} "
                f"is lower than source count {source_count}"
            )
        counts[source_table.name] = int(target_count)
        logger.info(
            "[RELATIONAL] %s.%s source=%d copied=%d target=%d",
            label,
            source_table.name,
            source_count,
            copied,
            target_count,
        )
    return counts


def _alias_names(client: QdrantClient) -> set[str]:
    return {alias.alias_name for alias in client.get_aliases().aliases}


def _create_alias(client: QdrantClient, alias: str, collection: str) -> None:
    if alias in _alias_names(client):
        return
    if not client.collection_exists(collection):
        raise RuntimeError(f"Cannot create alias {alias!r}; collection {collection!r} is absent.")
    client.update_collection_aliases(
        change_aliases_operations=[
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
            )
        ]
    )
    logger.info("[QDRANT] Created alias %s -> %s", alias, collection)


def _snapshot_qdrant(client: QdrantClient) -> None:
    for collection in client.get_collections().collections:
        try:
            snapshot = client.create_snapshot(collection.name)
            logger.info("[QDRANT] Snapshot created: %s/%s", collection.name, snapshot.name)
        except Exception as exc:
            logger.warning("[QDRANT] Skipping snapshot for %s due to: %s", collection.name, exc)


def _chunks(values: list, size: int) -> Iterable[list]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_").lower() or "legacy"


def _migrate_chroma(client: QdrantClient) -> dict[str, int]:
    global CHROMA_PATH
    src_chroma = "/migration-data/open-webui/vector_db"
    tmp_chroma = "/tmp/vector_db"
    
    if os.path.exists(src_chroma):
        import shutil
        if os.path.exists(tmp_chroma):
            shutil.rmtree(tmp_chroma)
        shutil.copytree(src_chroma, tmp_chroma)
        for root, dirs, files in os.walk(tmp_chroma):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o777)
            for f in files:
                os.chmod(os.path.join(root, f), 0o666)
        logger.info("[CHROMA] Copied %s to %s", src_chroma, tmp_chroma)
        CHROMA_PATH = Path(tmp_chroma)

    if not CHROMA_PATH.exists():
        return {}
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError("Chroma data exists but chromadb is unavailable in the migration image.") from exc

    chroma = chromadb.PersistentClient(path=str(CHROMA_PATH))
    migrated: dict[str, int] = {}
    for collection in chroma.list_collections():
        source = chroma.get_collection(collection.name)
        count = source.count()
        if count == 0:
            continue
        payload = source.get(include=["embeddings", "documents", "metadatas"])
        embeddings = payload.get("embeddings")
        if embeddings is None:
            embeddings = []
        if len(embeddings) != count:
            raise RuntimeError(
                f"Chroma collection {collection.name!r} has {count} rows but "
                f"{len(embeddings)} embeddings."
            )
        physical_name = f"legacy_chroma_{_safe_name(collection.name)}"
        alias_name = f"{physical_name}_active"
        dimension = len(embeddings[0])
        if not client.collection_exists(physical_name):
            client.create_collection(
                collection_name=physical_name,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
            )
        points = []
        ids = payload.get("ids")
        if ids is None:
            ids = []
        documents = payload.get("documents")
        if documents is None:
            documents = [None] * count
        metadatas = payload.get("metadatas")
        if metadatas is None:
            metadatas = [None] * count
        for source_id, vector, document, metadata in zip(ids, embeddings, documents, metadatas):
            points.append(
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"chroma:{collection.name}:{source_id}")),
                    vector=vector,
                    payload={
                        "legacy_chroma_id": source_id,
                        "document": document,
                        "metadata": metadata or {},
                    },
                )
            )
        for batch in _chunks(points, BATCH_SIZE):
            client.upsert(collection_name=physical_name, points=batch, wait=True)
        _create_alias(client, alias_name, physical_name)
        qdrant_count = client.count(collection_name=alias_name, exact=True).count
        if qdrant_count < count:
            raise RuntimeError(
                f"Chroma migration count mismatch for {collection.name!r}: "
                f"source={count}, qdrant={qdrant_count}"
            )
        migrated[collection.name] = qdrant_count
        logger.info("[CHROMA] %s -> %s count=%d", collection.name, alias_name, qdrant_count)
    return migrated


def main() -> None:
    import shutil
    
    src_webui = "/migration-data/open-webui/webui.db"
    tmp_webui = "/tmp/webui.db"
    if os.path.exists(src_webui):
        shutil.copy2(src_webui, tmp_webui)
        os.chmod(tmp_webui, 0o666)
        logger.info("[MIGRATION] Copied %s to %s", src_webui, tmp_webui)
        open_webui_url = f"sqlite:///{tmp_webui}"
    else:
        open_webui_url = OPEN_WEBUI_SQLITE_URL

    src_deepweb = "/migration-data/deep-web-mcp/auth_vault.db"
    tmp_deepweb = "/tmp/auth_vault.db"
    if os.path.exists(src_deepweb):
        shutil.copy2(src_deepweb, tmp_deepweb)
        os.chmod(tmp_deepweb, 0o666)
        logger.info("[MIGRATION] Copied %s to %s", src_deepweb, tmp_deepweb)
        deep_web_url = f"sqlite:///{tmp_deepweb}"
    else:
        deep_web_url = DEEP_WEB_SQLITE_URL

    _initialize_open_webui_schema()
    report = {
        "open_webui": _copy_relational(
            open_webui_url, OPEN_WEBUI_POSTGRES_URL, "open_webui"
        ),
        "deep_web": _copy_relational(deep_web_url, DEEP_WEB_POSTGRES_URL, "deep_web"),
    }
    qdrant = QdrantClient(url=QDRANT_URL)
    _snapshot_qdrant(qdrant)
    _create_alias(qdrant, NARRATIVE_ALIAS, NARRATIVE_COLLECTION)
    report["chroma"] = _migrate_chroma(qdrant)
    logger.info("[COMPLETE] %s", json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
