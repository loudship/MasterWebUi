"""Snapshot the current target and atomically switch a Qdrant alias."""

from __future__ import annotations

import logging
import os

from qdrant_client import QdrantClient, models

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("qdrant_alias_cutover")


def switch_alias(
    client: QdrantClient,
    *,
    alias_name: str,
    target_collection: str,
    snapshot_current: bool = True,
) -> None:
    if not client.collection_exists(target_collection):
        raise RuntimeError(f"Target collection {target_collection!r} does not exist.")

    aliases = {alias.alias_name: alias.collection_name for alias in client.get_aliases().aliases}
    current_collection = aliases.get(alias_name)
    if current_collection == target_collection:
        logger.info("[QDRANT] Alias %s already targets %s.", alias_name, target_collection)
        return

    if current_collection and snapshot_current:
        snapshot = client.create_snapshot(current_collection)
        logger.info("[QDRANT] Rollback snapshot created: %s/%s", current_collection, snapshot.name)

    operations = []
    if current_collection:
        operations.append(
            models.DeleteAliasOperation(
                delete_alias=models.DeleteAlias(alias_name=alias_name)
            )
        )
    operations.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(
                collection_name=target_collection,
                alias_name=alias_name,
            )
        )
    )
    client.update_collection_aliases(change_aliases_operations=operations)

    aliases = {alias.alias_name: alias.collection_name for alias in client.get_aliases().aliases}
    if aliases.get(alias_name) != target_collection:
        raise RuntimeError(
            f"Alias verification failed: {alias_name!r} does not target {target_collection!r}."
        )
    logger.info("[QDRANT] Atomic cutover complete: %s -> %s", alias_name, target_collection)


def main() -> None:
    alias_name = os.environ.get("QDRANT_ALIAS", "narrative_active")
    target_collection = os.environ["QDRANT_TARGET_COLLECTION"]
    snapshot_current = os.environ.get("QDRANT_SNAPSHOT_CURRENT", "true").lower() == "true"
    switch_alias(
        QdrantClient(url=os.environ.get("QDRANT_URL", "http://qdrant:6333")),
        alias_name=alias_name,
        target_collection=target_collection,
        snapshot_current=snapshot_current,
    )


if __name__ == "__main__":
    main()
