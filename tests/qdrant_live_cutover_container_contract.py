"""Validate uninterrupted alias reads during an atomic Qdrant cutover."""

from __future__ import annotations

import os
import sys
import threading
import time

from qdrant_client import QdrantClient, models

sys.path.insert(0, "/app/migration")
from qdrant_alias_cutover import switch_alias

url = os.environ["QDRANT_URL"]
alias = "contract_active"
primary = "contract_primary"
staged = "contract_staged"
writer = QdrantClient(url=url)

for collection in (primary, staged):
    writer.create_collection(
        collection_name=collection,
        vectors_config=models.VectorParams(size=1, distance=models.Distance.COSINE),
    )
writer.upsert(primary, [models.PointStruct(id=1, vector=[1.0], payload={"version": 1})])
writer.upsert(staged, [models.PointStruct(id=1, vector=[1.0], payload={"version": 2})])
writer.update_collection_aliases(
    change_aliases_operations=[
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=primary, alias_name=alias)
        )
    ]
)

versions: list[int] = []
errors: list[str] = []


def read_loop() -> None:
    reader = QdrantClient(url=url)
    for _ in range(150):
        try:
            points = reader.retrieve(collection_name=alias, ids=[1], with_payload=True)
            versions.append(points[0].payload["version"])
        except Exception as exc:
            errors.append(str(exc))
        time.sleep(0.005)


thread = threading.Thread(target=read_loop)
thread.start()
time.sleep(0.1)
switch_alias(writer, alias_name=alias, target_collection=staged)
thread.join()

assert not errors
assert set(versions) <= {1, 2}
assert writer.retrieve(collection_name=alias, ids=[1], with_payload=True)[0].payload["version"] == 2
print("qdrant-live-cutover-contract-ok")
