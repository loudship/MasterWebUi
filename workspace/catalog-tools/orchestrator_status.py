"""
title: Orchestrator Read-Only Status
author: Local Operations
version: 2.0.0
description: Read-only health posture for the local orchestrator and loaded models.
"""

import json
import urllib.error
import urllib.request
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        orchestrator_url: str = Field(default="http://langgraph-orchestrator:8100")
        lms_api_url: str = Field(default="http://host.docker.internal:4321/v1/models")

    def __init__(self):
        self.valves = self.Valves()

    def get_swarm_status(self) -> str:
        """Return read-only orchestrator health and active model IDs."""
        result = {"orchestrator": "offline", "models": [], "errors": []}
        try:
            with urllib.request.urlopen(f"{self.valves.orchestrator_url}/health", timeout=5) as response:
                result["orchestrator"] = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            result["errors"].append(f"orchestrator: {exc}")

        try:
            with urllib.request.urlopen(self.valves.lms_api_url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                result["models"] = [item.get("id") for item in payload.get("data", []) if item.get("id")]
        except (OSError, ValueError, urllib.error.URLError) as exc:
            result["errors"].append(f"models: {exc}")

        return json.dumps(result, indent=2, sort_keys=True)
