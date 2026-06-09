from types import SimpleNamespace

import pytest

pytest.importorskip("qdrant_client")

from scripts.qdrant_alias_cutover import switch_alias


class FakeQdrant:
    def __init__(self):
        self.alias_target = "primary_narrative"
        self.snapshots = []
        self.update_calls = []

    def collection_exists(self, name):
        return name == "narrative_v2"

    def get_aliases(self):
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(
                    alias_name="narrative_active",
                    collection_name=self.alias_target,
                )
            ]
        )

    def create_snapshot(self, collection):
        self.snapshots.append(collection)
        return SimpleNamespace(name="rollback.snapshot")

    def update_collection_aliases(self, *, change_aliases_operations):
        self.update_calls.append(change_aliases_operations)
        self.alias_target = "narrative_v2"


def test_atomic_alias_switch_snapshots_and_submits_one_update():
    client = FakeQdrant()
    switch_alias(
        client,
        alias_name="narrative_active",
        target_collection="narrative_v2",
    )
    assert client.snapshots == ["primary_narrative"]
    assert len(client.update_calls) == 1
    assert len(client.update_calls[0]) == 2
    assert client.alias_target == "narrative_v2"
