import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("asyncpg")
os.environ.setdefault("POSTGRES_OPS_URL", "postgresql://unused/unused")
os.environ.setdefault("MODEL_ALLOWLIST", "approved-model")

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "inference-gateway"
    / "inference_gateway.py"
)
spec = importlib.util.spec_from_file_location("inference_gateway", MODULE_PATH)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def test_model_allowlist_rejects_unknown_model(monkeypatch):
    monkeypatch.setattr(gateway, "MODEL_ALLOWLIST", {"approved-model"})
    with pytest.raises(gateway.HTTPException) as exc:
        gateway._validate_model({"model": "unknown-model"})
    assert exc.value.status_code == 403


def test_model_allowlist_accepts_approved_model(monkeypatch):
    monkeypatch.setattr(gateway, "MODEL_ALLOWLIST", {"approved-model"})
    assert gateway._validate_model({"model": "approved-model"}) == "approved-model"


def test_model_inventory_is_filtered_to_allowlist(monkeypatch):
    monkeypatch.setattr(gateway, "MODEL_ALLOWLIST", {"approved-model"})
    inventory = gateway._filter_model_inventory(
        {"data": [{"id": "approved-model"}, {"id": "blocked-model"}]}
    )
    assert inventory == {"data": [{"id": "approved-model"}]}
