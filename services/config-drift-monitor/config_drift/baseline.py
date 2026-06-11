from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


SECRET_ID = re.compile(r"(secret|token|password|api[-_]?key|credential|cookie)", re.IGNORECASE)
PRESENCE_MODES = {"present", "absent"}
COMPARE_MODES = {"exact", "ordered_list", "unordered_set", "subset", "present", "absent", "normalized_url", "fingerprint"}
PLANES = {"admin", "workspace", "user", "chat"}


class BaselineError(ValueError):
    pass


class BaselineLoader:
    def __init__(self, path: str):
        self.path = Path(path)
        self._mtime = -1.0
        self._baseline: dict[str, Any] | None = None

    def load(self, force: bool = False) -> dict[str, Any]:
        mtime = self.path.stat().st_mtime
        if not force and self._baseline is not None and mtime == self._mtime:
            return self._baseline
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        validate_baseline(data)
        self._baseline = data
        self._mtime = mtime
        return data


def validate_baseline(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 1:
        raise BaselineError("Baseline schema_version must be 1.")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise BaselineError("Baseline must contain at least one rule.")
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise BaselineError("Each baseline rule must be an object.")
        rule_id = str(rule.get("id") or "")
        if not rule_id or rule_id in seen:
            raise BaselineError("Every baseline rule must have a unique id.")
        seen.add(rule_id)
        mode = rule.get("mode", "exact")
        if mode not in COMPARE_MODES:
            raise BaselineError(f"Rule {rule_id} has unsupported comparison mode {mode}.")
        sources = rule.get("sources")
        if not isinstance(sources, dict) or not sources:
            raise BaselineError(f"Rule {rule_id} must map at least one source plane.")
        if set(sources) - PLANES:
            raise BaselineError(f"Rule {rule_id} contains an unsupported source plane.")
        for allowlist_key in ("allowlist", "entity_allowlist"):
            if allowlist_key in rule and not isinstance(rule[allowlist_key], list):
                raise BaselineError(f"Rule {rule_id} {allowlist_key} must be a list.")
        if SECRET_ID.search(rule_id) and mode not in PRESENCE_MODES:
            raise BaselineError(f"Secret-like rule {rule_id} may only use present/absent comparison.")
        if SECRET_ID.search(str(rule.get("logical_path", ""))) and mode not in PRESENCE_MODES:
            raise BaselineError(f"Secret-like rule {rule_id} may only use present/absent comparison.")
        if mode not in PRESENCE_MODES and _contains_literal_secret(rule.get("expected")):
            raise BaselineError(f"Rule {rule_id} contains a literal secret-like expected value.")


def _contains_literal_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(SECRET_ID.search(str(key)) or _contains_literal_secret(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_literal_secret(child) for child in value)
    if isinstance(value, str):
        lowered = value.lower()
        return lowered.startswith(("sk-", "bearer ", "token=", "password="))
    return False
