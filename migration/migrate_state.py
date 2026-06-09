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
from sqlalchemy import MetaData, create_engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration")

OPEN_WEBUI_SQLITE_URL = os.environ.get(
    "OPEN_WEBUI_SQLITE_URL", "sqlite:////migration-data/open-webui/webui.db"
)
OPEN_WEBUI_POSTGRES_URL = os.environ["OPEN_WEBUI_POSTGRES_URL"]
DEEP_WEB_SQLITE_URL = os.environ.get(
    "DEEP_WEB_SQLITE_URL", "sqlite:////migration-data/deep-web-mcp/auth_vault.db"
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
    os.environ["DATABASE_URL"] = OPEN_WEBUI_POSTGRES_URL
    os.environ["ENABLE_DB_MIGRATIONS"] = "true"
    import open_webui.config  # noqa: F401


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
            source_table.to_metadata(target_metadata)
            target_metadata.tables[source_table.name].create(bind=target, checkfirst=True)
        target_table = target_metadata.tables[source_table.name]
        primary_keys = [column.name for column in target_table.primary_key.columns]
        copied = 0

        with source.connect() as source_connection, target.begin() as target_connection:
            result = source_connection.execute(select(source_table))
            while True:
                rows = result.mappings().fetchmany(BATCH_SIZE)
                if not rows:
                    break
                values = [dict(row) for row in rows]
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
        snapshot = client.create_snapshot(collection.name)
        logger.info("[QDRANT] Snapshot created: %s/%s", collection.name, snapshot.name)


def _chunks(values: list, size: int) -> Iterable[list]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_").lower() or "legacy"


def _migrate_chroma(client: QdrantClient) -> dict[str, int]:
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
        embeddings = payload.get("embeddings") or []
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
        ids = payload.get("ids") or []
        documents = payload.get("documents") or [None] * count
        metadatas = payload.get("metadatas") or [None] * count
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
    _initialize_open_webui_schema()
    report = {
        "open_webui": _copy_relational(
            OPEN_WEBUI_SQLITE_URL, OPEN_WEBUI_POSTGRES_URL, "open_webui"
        ),
        "deep_web": _copy_relational(DEEP_WEB_SQLITE_URL, DEEP_WEB_POSTGRES_URL, "deep_web"),
    }
    qdrant = QdrantClient(url=QDRANT_URL)
    _snapshot_qdrant(qdrant)
    _create_alias(qdrant, NARRATIVE_ALIAS, NARRATIVE_COLLECTION)
    report["chroma"] = _migrate_chroma(qdrant)
    logger.info("[COMPLETE] %s", json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
