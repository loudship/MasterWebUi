#!/usr/bin/env python3
"""Run reversible authenticated CRUD probes against the five Workspace pillars."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class Api:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                content = response.read()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {exc.read().decode()}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    api = Api(args.base_url, args.token)
    suffix = str(int(time.time()))
    model_id = f"workspace-audit-model-{suffix}"
    prompt_command = f"workspace-audit-prompt-{suffix}"
    tool_id = f"workspace_audit_tool_{suffix}"
    function_id = f"workspace_audit_function_{suffix}"
    created: dict[str, str] = {}
    results: dict[str, str] = {}

    try:
        base = next(item for item in api.request("GET", "/api/v1/models/export") if item["id"] == "qwen35")
        model = api.request(
            "POST",
            "/api/v1/models/create",
            {
                "id": model_id,
                "base_model_id": base.get("base_model_id"),
                "name": "Workspace Audit Model Probe",
                "meta": {},
                "params": {},
                "access_grants": [],
                "is_active": True,
            },
        )
        created["model"] = model_id
        model["name"] = "Workspace Audit Model Probe Updated"
        api.request("POST", "/api/v1/models/model/update", model)
        assert any(item["id"] == model_id and item["name"].endswith("Updated") for item in api.request("GET", "/api/v1/models/export"))
        results["model"] = "PASS"

        prompt = api.request(
            "POST",
            "/api/v1/prompts/create",
            {
                "command": prompt_command,
                "name": "Workspace Audit Prompt Probe",
                "content": "Probe {{topic}}",
                "data": {},
                "meta": {"description": "reversible audit probe"},
                "access_grants": [],
                "commit_message": "Create audit probe",
                "is_production": True,
            },
        )
        created["prompt"] = prompt["id"]
        prompt["name"] = "Workspace Audit Prompt Probe Updated"
        prompt["commit_message"] = "Update audit probe"
        api.request("POST", f"/api/v1/prompts/id/{prompt['id']}/update", prompt)
        assert api.request("GET", f"/api/v1/prompts/id/{prompt['id']}")["name"].endswith("Updated")
        results["prompt"] = "PASS"

        knowledge = api.request(
            "POST",
            "/api/v1/knowledge/create",
            {"name": "Workspace Audit Knowledge Probe", "description": "reversible audit probe", "access_grants": []},
        )
        created["knowledge"] = knowledge["id"]
        api.request(
            "POST",
            f"/api/v1/knowledge/{knowledge['id']}/update",
            {"name": "Workspace Audit Knowledge Probe Updated", "description": "updated", "access_grants": []},
        )
        assert api.request("GET", f"/api/v1/knowledge/{knowledge['id']}")["name"].endswith("Updated")
        results["knowledge"] = "PASS"

        tool_content = 'class Tools:\n    def probe(self, value: str = "ok") -> str:\n        """Return a bounded audit probe value."""\n        return value[:32]\n'
        tool = api.request(
            "POST",
            "/api/v1/tools/create",
            {"id": tool_id, "name": "Workspace Audit Tool Probe", "content": tool_content, "meta": {"description": "probe", "manifest": {}}, "access_grants": []},
        )
        created["tool"] = tool_id
        api.request(
            "POST",
            f"/api/v1/tools/id/{tool_id}/update",
            {
                "id": tool_id,
                "name": "Workspace Audit Tool Probe Updated",
                "content": tool_content,
                "meta": tool["meta"],
                "access_grants": tool["access_grants"],
            },
        )
        assert api.request("GET", f"/api/v1/tools/id/{tool_id}")["name"].endswith("Updated")
        results["tool"] = "PASS"

        function_content = 'class Filter:\n    async def inlet(self, body: dict) -> dict:\n        return body\n'
        function = api.request(
            "POST",
            "/api/v1/functions/create",
            {"id": function_id, "name": "Workspace Audit Function Probe", "content": function_content, "meta": {"description": "probe", "manifest": {}}, "access_grants": []},
        )
        created["function"] = function_id
        api.request(
            "POST",
            f"/api/v1/functions/id/{function_id}/update",
            {
                "id": function_id,
                "name": "Workspace Audit Function Probe Updated",
                "content": function_content,
                "meta": function["meta"],
                "access_grants": function.get("access_grants") or [],
            },
        )
        assert api.request("GET", f"/api/v1/functions/id/{function_id}")["name"].endswith("Updated")
        results["function"] = "PASS"
    finally:
        if "function" in created:
            api.request("DELETE", f"/api/v1/functions/id/{function_id}/delete")
        if "tool" in created:
            api.request("DELETE", f"/api/v1/tools/id/{tool_id}/delete")
        if "knowledge" in created:
            api.request("DELETE", f"/api/v1/knowledge/{created['knowledge']}/delete")
        if "prompt" in created:
            api.request("DELETE", f"/api/v1/prompts/id/{created['prompt']}/delete")
        if "model" in created:
            api.request("POST", "/api/v1/models/model/delete", {"id": model_id})

    residue = {
        "model": any(item["id"] == model_id for item in api.request("GET", "/api/v1/models/export")),
        "prompt": any(item["command"] == prompt_command for item in api.request("GET", "/api/v1/prompts/")),
        "tool": any(item["id"] == tool_id for item in api.request("GET", "/api/v1/tools/export")),
        "function": any(item["id"] == function_id for item in api.request("GET", "/api/v1/functions/export")),
    }
    if any(residue.values()):
        raise RuntimeError(f"Probe residue detected: {residue}")
    print(json.dumps({"results": results, "residue": residue}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
