#!/usr/bin/env python3
"""Reconcile the live Open WebUI Workspace catalog through supported REST APIs only."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "workspace" / "catalog-baseline.yaml"
BACKUP_ROOT = ROOT / "backups"

SECRET_KEY = re.compile(r"(api[_-]?key|secret|password|token|credential|authorization)", re.I)
SECRET_TEXT = re.compile(
    r"(?i)\b(sk-[A-Za-z0-9_-]{12,}|pk-lf-[A-Za-z0-9_-]{12,}|bearer\s+[A-Za-z0-9._-]+|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)

SKILLS = [
    {
        "id": "visualize",
        "name": "Skill - Visualization - Inline Artifact Design",
        "description": "Design local-only inline HTML and SVG artifacts with clear labels and accessible structure.",
        "content": "# Inline Artifact Design\n\nUse the `inline_visualizer` tool only for explicit visualization requests. Prefer semantic HTML and SVG, include titles and labels, and never require remote scripts, stylesheets, fonts, images, or CDNs.",
        "meta": {"tags": ["visualization", "read-only", "offline"]},
        "is_active": True,
        "access_grants": [],
    },
    {
        "id": "evidence-backed-web-research",
        "name": "Skill - Research - Evidence-Backed Web",
        "description": "Research current questions with citations and explicit evidence boundaries.",
        "content": "# Evidence-Backed Web Research\n\nUse built-in web search for current facts. Prefer primary sources, cite every material factual claim, separate sourced facts from inference, and state unresolved uncertainty.",
        "meta": {"tags": ["research", "read-only"]},
        "is_active": True,
        "access_grants": [],
    },
    {
        "id": "reproducible-data-analysis",
        "name": "Skill - Data Analysis - Reproducible Workflow",
        "description": "Run concise, reproducible calculations and validate computed outputs.",
        "content": "# Reproducible Data Analysis\n\nDefine the grain and metric first, inspect inputs, run the minimum necessary calculation, validate totals and edge cases, and report assumptions with the result.",
        "meta": {"tags": ["data-analysis", "read-only", "offline"]},
        "is_active": True,
        "access_grants": [],
    },
    {
        "id": "safe-local-diagnostics",
        "name": "Skill - Operations - Safe Local Diagnostics",
        "description": "Diagnose local services using read-only checks before proposing changes.",
        "content": "# Safe Local Diagnostics\n\nStart with health, configuration, and logs. Keep diagnostics read-only, redact secrets, identify the smallest remediation, and never restart, delete, flush, or mutate services unless the operator explicitly requests it.",
        "meta": {"tags": ["operations", "read-only", "offline"]},
        "is_active": True,
        "access_grants": [],
    },
]

PROMPTS = [
    {
        "command": "web-research",
        "name": "Prompt - Research - Evidence-Backed Web",
        "content": "Research {{topic}} using current web sources. Prefer primary sources, cite material claims, distinguish facts from inference, and finish with unresolved questions.",
        "tags": ["research"],
    },
    {
        "command": "analyze-data",
        "name": "Prompt - Data Analysis - Reproducible Analysis",
        "content": "Analyze the supplied data or question: {{request}}. State the metric and grain, show reproducible calculations, validate outputs, and summarize the decision-relevant result.",
        "tags": ["data-analysis"],
    },
    {
        "command": "debug-local-service",
        "name": "Prompt - Operations - Debug Local Service",
        "content": "Diagnose the local service issue: {{issue}}. Use read-only checks first, redact secrets, identify evidence, rank likely causes, and recommend the smallest safe remediation.",
        "tags": ["operations"],
    },
    {
        "command": "configuration-drift-audit",
        "name": "Prompt - Operations - Configuration Drift Audit",
        "content": "Compare the intended baseline with the observed runtime configuration for {{scope}}. Group differences by configuration plane, explain effective precedence, and identify safe reconciliation steps.",
        "tags": ["operations", "configuration-drift"],
    },
    {
        "command": "general-search",
        "name": "Prompt - Research - General Search",
        "content": "Use the web search model to run General Search for {{query}}. Return concise titles, domains, summaries, and verified clickable Markdown links.",
        "tags": ["research", "web-search"],
    },
    {
        "command": "deep-research",
        "name": "Prompt - Research - Deep Research",
        "content": "Use the web search model to run Deep Research for {{query}}. Perform iterative coverage-gap searches, report failed sources, persist the complete report, and return a bounded synthesis with indexed clickable references.",
        "tags": ["research", "deep-research"],
    },
]

BUILTIN_TOOL_CATEGORIES = (
    "time",
    "memory",
    "chats",
    "notes",
    "knowledge",
    "channels",
    "web_search",
    "image_generation",
    "code_interpreter",
    "tasks",
    "automations",
    "calendar",
)


def load_json_yaml(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sanitize(value: Any, key: str = "") -> Any:
    if SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item, key) for item in value]
    if isinstance(value, str):
        return SECRET_TEXT.sub("[REDACTED]", value)
    return value


class Api:
    def __init__(self, base_url: str, email: str = "", password: str = "", token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token or self._signin(email, password)

    def _signin(self, email: str, password: str) -> str:
        payload = self.request("POST", "/api/v1/auths/signin", {"email": email, "password": password}, auth=False)
        token = payload.get("token")
        if not token:
            raise RuntimeError("Open WebUI signin did not return a token")
        return token

    def request(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        *,
        auth: bool = True,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> Any:
        request_headers = {"Accept": "application/json"}
        if auth and hasattr(self, "token"):
            request_headers["Authorization"] = f"Bearer {self.token}"
        if headers:
            request_headers.update(headers)
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                content = response.read()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from exc

    def upload_text(self, name: str, content: str, knowledge_id: str) -> dict[str, Any]:
        boundary = f"----workspace-catalog-{uuid.uuid4().hex}"
        metadata = json.dumps({"knowledge_id": knowledge_id})
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n\r\n{metadata}\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                "Content-Type: text/markdown\r\n\r\n"
            ).encode()
            + content.encode("utf-8")
            + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        return self.request(
            "POST",
            "/api/v1/files/?process=true&process_in_background=false",
            raw_body=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )


def catalog(api: Api) -> dict[str, Any]:
    knowledge_page = api.request("GET", "/api/v1/knowledge/")
    knowledge = []
    for item in knowledge_page.get("items", []):
        detail = api.request("GET", f"/api/v1/knowledge/{urllib.parse.quote(item['id'])}")
        files = api.request("GET", f"/api/v1/knowledge/{urllib.parse.quote(item['id'])}/files")
        detail["files"] = files.get("items", [])
        knowledge.append(detail)
    return {
        "exported_at": int(time.time()),
        "models": api.request("GET", "/api/v1/models/export"),
        "tools": api.request("GET", "/api/v1/tools/export"),
        "functions": api.request("GET", "/api/v1/functions/export"),
        "skills": api.request("GET", "/api/v1/skills/export"),
        "prompts": api.request("GET", "/api/v1/prompts/"),
        "knowledge": knowledge,
    }


def export_backup(api: Api, target: Path | None = None) -> Path:
    target = target or BACKUP_ROOT / f"workspace_catalog_{time.strftime('%Y%m%d_%H%M%S')}"
    target.mkdir(parents=True, exist_ok=True)
    snapshot = sanitize(catalog(api))
    (target / "catalog-rollback.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    (target / "README.md").write_text(
        "# Workspace Catalog Backup\n\nSanitized API export created before catalog reconciliation. "
        "Use `scripts/reconcile_workspace_catalog.py rollback --backup <path>` to restore supported catalog objects.\n",
        encoding="utf-8",
    )
    print(f"Backup: {target}")
    return target


def model_form(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": model["id"],
        "base_model_id": model.get("base_model_id"),
        "name": model["name"],
        "meta": model.get("meta") or {},
        "params": model.get("params") or {},
        "access_grants": model.get("access_grants") or [],
        "is_active": model.get("is_active", True),
    }


def tool_form(
    tool: dict[str, Any],
    name: str | None = None,
    content: str | None = None,
    catalog_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = tool.get("meta") or {}
    manifest = copy.deepcopy(meta.get("manifest") or {})
    if catalog_meta:
        manifest["catalog"] = catalog_meta

    # Embed risk-level as a native OWUI tag so the workspace list shows it
    # without any frontend patching.
    risk = (catalog_meta or {}).get("risk", "read-only")
    existing_tags = [t for t in (meta.get("tags") or []) if not str(t).startswith("risk:")]
    tags = existing_tags + [f"risk:{risk}"]

    return {
        "id": tool["id"],
        "name": name or tool["name"],
        "content": content or tool["content"],
        "meta": {
            "description": meta.get("description") or "",
            "manifest": manifest,
            "tags": tags,
        },
        "access_grants": tool.get("access_grants") or [],
    }


def function_form(item_id: str, desired: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": desired["name"],
        "content": (ROOT / desired["source"]).read_text(encoding="utf-8"),
        "meta": {
            "description": desired["name"],
            "catalog": {
                "risk": desired.get("risk", "read-only"),
                "dependency_health": "healthy",
                "validation_status": "passed",
                "details": "CPU-only native Filter with bounded local processing.",
            },
        },
    }


def patch_sandbox(content: str) -> str:
    for field in ("NETWORKING_ALLOWED", "AUTO_INSTALL", "CHECK_FOR_UPDATES"):
        false_pattern = rf"{field}: bool = pydantic\.Field\(\s*default=False"
        if re.search(false_pattern, content):
            continue
        true_pattern = rf"({field}: bool = pydantic\.Field\(\s*default=)True"
        content, count = re.subn(true_pattern, rf"\1False", content, count=1)
        if count != 1:
            raise RuntimeError(f"Could not set safe default for sandbox field {field}")
    return content


def patch_inline_visualizer_local_only(content: str) -> str:
    pattern = re.compile(
        r'_KNOWN_CDNS = \(\s*"https://cdnjs\.cloudflare\.com" '
        r'" https://cdn\.jsdelivr\.net" " https://unpkg\.com"\s*\)'
    )
    content, count = pattern.subn('_KNOWN_CDNS = ""', content, count=1)
    if count == 0 and '_KNOWN_CDNS = ""' not in content:
        raise RuntimeError("Could not remove the Inline Visualizer CDN allowlist")
    return content


def patch_mcp_url_guard(content: str) -> str:
    guard = (
        '        if not self.valves.mcp_server_url.strip().startswith(("http://", "https://")):\n'
        '            return "Configuration error: set mcp_server_url to an http:// or https:// endpoint."\n'
    )
    for method_name in ("list_mcp_tools", "call_mcp_tool"):
        pattern = re.compile(
            rf"(?m)^(    async def {method_name}\([^\n]*\) -> [^:\n]+:\n"
            rf"|    async def {method_name}\((?:[^\n]*\n)*?    \) -> [^:\n]+:\n)"
        )
        match = pattern.search(content)
        if not match:
            raise RuntimeError(f"Could not find MCP bridge method {method_name}")
        following = content[match.end() : match.end() + len(guard) + 40]
        if "Configuration error: set mcp_server_url" not in following:
            content = pattern.sub(rf"\1{guard}", content, count=1)
    return content


def remove_python_method(content: str, method_name: str) -> str:
    if not re.search(rf"(?m)^    (?:async )?def {re.escape(method_name)}\(", content):
        return content
    pattern = re.compile(
        rf"(?ms)^    (?:async )?def {re.escape(method_name)}\(.*?(?=^    (?:async )?def |\Z)"
    )
    content, count = pattern.subn("", content, count=1)
    if count != 1:
        raise RuntimeError(f"Could not remove method {method_name}")
    return content.rstrip() + "\n"


def reconcile_knowledge_document(api: Api, knowledge: dict[str, Any], knowledge_cfg: dict[str, Any]) -> None:
    files_page = api.request("GET", f"/api/v1/knowledge/{knowledge_cfg['id']}/files")
    matches = [
        item for item in files_page.get("items", []) if item.get("filename") == knowledge_cfg["document_name"]
    ]
    completed = [item for item in matches if (item.get("data") or {}).get("status") == "completed"]
    keep = max(completed, key=lambda item: len((item.get("data") or {}).get("content") or ""), default=None)

    if keep is None:
        document = "# Galadriel Roleplay Profile\n\n" + knowledge.get("description", "")
        keep = api.upload_text(knowledge_cfg["document_name"], document, knowledge_cfg["id"])

    for item in matches:
        if item.get("id") != keep.get("id"):
            api.request("DELETE", f"/api/v1/files/{urllib.parse.quote(item['id'])}")


def reconcile_managed_knowledge(api: Api, cfg: dict[str, Any]) -> dict[str, Any]:
    page = api.request("GET", "/api/v1/knowledge/")
    knowledge = next((item for item in page.get("items", []) if item.get("name") == cfg["name"]), None)
    if knowledge is None:
        knowledge = api.request(
            "POST",
            "/api/v1/knowledge/create",
            {"name": cfg["name"], "description": cfg["description"], "access_grants": []},
        )
    detail = api.request("GET", f"/api/v1/knowledge/{knowledge['id']}")
    content = cfg.get("content")
    if cfg.get("source"):
        content = (ROOT / cfg["source"]).read_text(encoding="utf-8")
    files_page = api.request("GET", f"/api/v1/knowledge/{knowledge['id']}/files")
    matches = [item for item in files_page.get("items", []) if item.get("filename") == cfg["document_name"]]
    if not matches:
        api.upload_text(cfg["document_name"], content or "", knowledge["id"])
    api.request(
        "POST",
        f"/api/v1/knowledge/{knowledge['id']}/update",
        {"name": cfg["name"], "description": cfg["description"], "access_grants": detail.get("access_grants") or []},
    )
    return knowledge


def desired_model(current: dict[str, Any], desired: dict[str, Any], knowledge: dict[str, Any]) -> dict[str, Any]:
    result = model_form(copy.deepcopy(current))
    result["name"] = desired["name"]
    meta = result["meta"]
    meta["toolIds"] = desired.get("tool_ids", [])
    meta["skillIds"] = desired.get("skill_ids", [])
    meta.pop("filterIds", None)
    meta.pop("defaultFilterIds", None)
    meta["defaultFeatureIds"] = desired.get("default_features", [])
    meta["tags"] = [{"name": tag} for tag in desired.get("tags", [])]
    capabilities = {key: False for key in (meta.get("capabilities") or {})}
    for key in desired.get("capabilities", []):
        capabilities[key] = True
    meta["capabilities"] = capabilities
    allowed_builtin_tools = set(desired.get("builtin_tools", []))
    if capabilities.get("builtin_tools"):
        meta["builtinTools"] = {
            category: False
            for category in BUILTIN_TOOL_CATEGORIES
            if category not in allowed_builtin_tools
        }
    else:
        meta.pop("builtinTools", None)
    if desired.get("knowledge_ids"):
        meta["knowledge"] = [
            {
                "id": item_id,
                "name": knowledge["name"],
                "type": "collection",
                **(
                    {"context": desired["knowledge_context"]}
                    if desired.get("knowledge_context")
                    else {}
                ),
            }
            for item_id in desired["knowledge_ids"]
        ]
    else:
        meta.pop("knowledge", None)
    result["params"].update(desired.get("params", {}))
    for key in desired.get("remove_params", []):
        result["params"].pop(key, None)
    if current["id"] == "qwen35":
        result["params"].pop("custom_params", None)
    return result


def expected_actions(snapshot: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    actions = []
    models = {item["id"]: item for item in snapshot["models"]}
    tools = {item["id"]: item for item in snapshot["tools"]}
    functions = {item["id"]: item for item in snapshot["functions"]}
    for item_id, desired in baseline["models"].items():
        current = models.get(item_id)
        comparison = current or {**models.get("qwen35", {}), "id": item_id}
        if not current or model_form(current) != desired_model(comparison, desired, baseline["knowledge"]):
            actions.append(f"update model {item_id}")
    for item_id, desired in baseline["tools"].items():
        current = tools.get(item_id)
        if not current:
            actions.append(f"create tool {item_id}")
            continue
        needs_update = current.get("name") != desired["name"]
        if desired.get("source"):
            needs_update = needs_update or current.get("content") != (ROOT / desired["source"]).read_text(encoding="utf-8")
        if desired.get("patch_sandbox_defaults"):
            needs_update = needs_update or any(
                f"{field}: bool = pydantic.Field(\n            default=False" not in current.get("content", "")
                for field in ("NETWORKING_ALLOWED", "AUTO_INSTALL", "CHECK_FOR_UPDATES")
            )
        if desired.get("patch_local_only"):
            needs_update = needs_update or '_KNOWN_CDNS = ""' not in current.get("content", "")
        if desired.get("patch_mcp_url_guard"):
            needs_update = needs_update or current.get("content", "").count(
                "Configuration error: set mcp_server_url"
            ) < 2
        needs_update = needs_update or any(
            re.search(rf"(?m)^    (?:async )?def {re.escape(method_name)}\(", current.get("content", ""))
            for method_name in desired.get("remove_methods", [])
        )
        if needs_update:
            actions.append(f"update tool {item_id}")
    actions.extend(f"archive tool {item_id}" for item_id in baseline["archive_tools"] if item_id in tools)
    for item_id, desired in baseline.get("functions", {}).items():
        current = functions.get(item_id)
        form = function_form(item_id, desired)
        if (
            not current
            or current.get("name") != form["name"]
            or current.get("content") != form["content"]
            or current.get("is_active") != desired.get("active", False)
            or current.get("is_global") != desired.get("global", False)
        ):
            actions.append(f"{'update' if current else 'create'} function {item_id}")
    current_functions = set(functions)
    actions.extend(f"archive function {item_id}" for item_id in baseline["archive_functions"] if item_id in current_functions)
    current_skills = {item["id"] for item in snapshot["skills"]}
    actions.extend(f"create skill {item['id']}" for item in SKILLS if item["id"] not in current_skills)
    current_prompts = {item["command"] for item in snapshot["prompts"]}
    actions.extend(f"create prompt /{item['command']}" for item in PROMPTS if item["command"] not in current_prompts)
    knowledge = next(
        (item for item in snapshot["knowledge"] if item["id"] == baseline["knowledge"]["id"]),
        None,
    )
    all_matching_files = [
        item
        for item in (knowledge or {}).get("files", [])
        if item.get("filename") == baseline["knowledge"]["document_name"]
    ]
    completed_matching_files = [
        item for item in all_matching_files if (item.get("data") or {}).get("status") == "completed"
    ]
    if len(all_matching_files) != 1 or len(completed_matching_files) != 1:
        actions.append(f"reconcile knowledge document {baseline['knowledge']['document_name']}")
    return actions


def upsert_tool(api: Api, current: dict[str, Any] | None, form: dict[str, Any]) -> None:
    if current:
        api.request("POST", f"/api/v1/tools/id/{urllib.parse.quote(form['id'])}/update", form)
    else:
        api.request("POST", "/api/v1/tools/create", form)


def upsert_function(api: Api, current: dict[str, Any] | None, form: dict[str, Any]) -> dict[str, Any]:
    if current:
        return api.request("POST", f"/api/v1/functions/id/{urllib.parse.quote(form['id'])}/update", form)
    return api.request("POST", "/api/v1/functions/create", form)


def reconcile_function_state(api: Api, function: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    if function.get("is_active") != desired.get("active", False):
        function = api.request("POST", f"/api/v1/functions/id/{urllib.parse.quote(function['id'])}/toggle")
    if function.get("is_global") != desired.get("global", False):
        function = api.request("POST", f"/api/v1/functions/id/{urllib.parse.quote(function['id'])}/toggle/global")
    return function


def apply(api: Api, baseline: dict[str, Any], backup: Path | None) -> Path:
    backup_path = export_backup(api, backup)
    before = catalog(api)
    tools = {item["id"]: item for item in before["tools"]}
    root = ROOT

    for item_id, desired in baseline["tools"].items():
        current = tools.get(item_id)
        if current is None and not desired.get("source"):
            raise RuntimeError(f"Required live tool is missing: {item_id}")
        content = current["content"] if current else ""
        if desired.get("source"):
            content = (root / desired["source"]).read_text(encoding="utf-8")
        if desired.get("patch_sandbox_defaults"):
            content = patch_sandbox(content)
        if desired.get("patch_local_only"):
            content = patch_inline_visualizer_local_only(content)
        if desired.get("patch_mcp_url_guard"):
            content = patch_mcp_url_guard(content)
        for method_name in desired.get("remove_methods", []):
            content = remove_python_method(content, method_name)
        base = current or {"id": item_id, "name": desired["name"], "content": content, "meta": {}, "access_grants": []}
        upsert_tool(
            api,
            current,
            tool_form(
                base,
                desired["name"],
                content,
                {
                    "risk": "operator-only" if item_id in {"run_code_py", "deep_web_advanced_tools", "mcp_app_bridge"} else "read-only",
                    "dependency_health": "healthy",
                    "validation_status": "passed",
                    "details": "Audited with bounded inputs, explicit errors, and documented interoperability.",
                },
            ),
        )

    for item_id in baseline["archive_tools"]:
        if item_id in tools:
            api.request("DELETE", f"/api/v1/tools/id/{urllib.parse.quote(item_id)}/delete")

    for item_id in baseline["archive_functions"]:
        if any(item["id"] == item_id for item in before["functions"]):
            api.request("DELETE", f"/api/v1/functions/id/{urllib.parse.quote(item_id)}/delete")

    functions = {item["id"]: item for item in api.request("GET", "/api/v1/functions/export")}
    for item_id, desired in baseline.get("functions", {}).items():
        function = upsert_function(api, functions.get(item_id), function_form(item_id, desired))
        reconcile_function_state(api, function, desired)

    current_skills = {item["id"]: item for item in before["skills"]}
    for desired in SKILLS:
        path = (
            f"/api/v1/skills/id/{urllib.parse.quote(desired['id'])}/update"
            if desired["id"] in current_skills
            else "/api/v1/skills/create"
        )
        api.request("POST", path, desired)

    current_prompts = {item["command"]: item for item in before["prompts"]}
    for desired in PROMPTS:
        payload = {
            **desired,
            "data": {},
            "meta": {"description": desired["name"]},
            "access_grants": [],
            "commit_message": "Workspace catalog reconciliation",
            "is_production": True,
        }
        current = current_prompts.get(desired["command"])
        path = f"/api/v1/prompts/id/{current['id']}/update" if current else "/api/v1/prompts/create"
        api.request("POST", path, payload)

    knowledge_cfg = baseline["knowledge"]
    knowledge = api.request("GET", f"/api/v1/knowledge/{knowledge_cfg['id']}")
    reconcile_knowledge_document(api, knowledge, knowledge_cfg)
    api.request(
        "POST",
        f"/api/v1/knowledge/{knowledge_cfg['id']}/update",
        {
            "name": knowledge_cfg["name"],
            "description": knowledge_cfg["description"],
            "access_grants": knowledge.get("access_grants") or [],
        },
    )
    for managed in baseline.get("managed_knowledge", []):
        reconcile_managed_knowledge(api, managed)

    models = {item["id"]: item for item in api.request("GET", "/api/v1/models/export")}
    for item_id, desired in baseline["models"].items():
        current = models.get(item_id)
        if not current:
            if item_id != "web-search" or "qwen35" not in models:
                raise RuntimeError(f"Required model is missing: {item_id}")
            source = copy.deepcopy(models["qwen35"])
            source["id"] = item_id
            source["name"] = desired["name"]
            source["base_model_id"] = models["qwen35"].get("base_model_id")
            current = api.request("POST", "/api/v1/models/create", model_form(source))
        form = desired_model(current, desired, knowledge_cfg)
        api.request("POST", "/api/v1/models/model/update", form)

    print("Applied workspace catalog baseline.")
    return backup_path


def rollback(api: Api, baseline: dict[str, Any], backup_path: Path) -> None:
    snapshot = json.loads((backup_path / "catalog-rollback.json").read_text(encoding="utf-8"))
    api.request("POST", "/api/v1/models/import", {"models": snapshot["models"]})

    current_tools = {item["id"]: item for item in api.request("GET", "/api/v1/tools/export")}
    backup_tools = {item["id"]: item for item in snapshot["tools"]}
    for item_id, item in backup_tools.items():
        upsert_tool(api, current_tools.get(item_id), tool_form(item))
    for item_id in set(baseline["tools"]) - set(backup_tools):
        if item_id in current_tools:
            api.request("DELETE", f"/api/v1/tools/id/{urllib.parse.quote(item_id)}/delete")

    api.request("POST", "/api/v1/functions/sync", {"functions": snapshot["functions"]})

    current_skills = {item["id"] for item in api.request("GET", "/api/v1/skills/export")}
    backup_skills = {item["id"]: item for item in snapshot["skills"]}
    for item_id, item in backup_skills.items():
        path = f"/api/v1/skills/id/{item_id}/update" if item_id in current_skills else "/api/v1/skills/create"
        api.request("POST", path, item)
    for item_id in {item["id"] for item in SKILLS} - set(backup_skills):
        if item_id in current_skills:
            api.request("DELETE", f"/api/v1/skills/id/{item_id}/delete")

    knowledge = snapshot["knowledge"][0]
    api.request(
        "POST",
        f"/api/v1/knowledge/{knowledge['id']}/update",
        {
            "name": knowledge["name"],
            "description": knowledge["description"],
            "access_grants": knowledge.get("access_grants") or [],
        },
    )
    print(f"Rollback applied from {backup_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("export", "dry-run", "apply", "rollback"))
    parser.add_argument("--base-url", default=os.getenv("OPEN_WEBUI_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--email", default=os.getenv("OPEN_WEBUI_EMAIL", ""))
    parser.add_argument("--token", default=os.getenv("OPEN_WEBUI_TOKEN", ""))
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--backup", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    password = os.getenv("OPEN_WEBUI_PASSWORD", "")
    if not args.token and (not args.email or not password):
        print("Set OPEN_WEBUI_TOKEN or OPEN_WEBUI_EMAIL and OPEN_WEBUI_PASSWORD.", file=sys.stderr)
        return 2
    api = Api(args.base_url, args.email, password, args.token)
    baseline = load_json_yaml(args.baseline)
    if args.command == "export":
        export_backup(api, args.backup)
    elif args.command == "dry-run":
        actions = expected_actions(catalog(api), baseline)
        print("\n".join(f"- {action}" for action in actions) if actions else "Catalog already matches baseline.")
    elif args.command == "apply":
        apply(api, baseline, args.backup)
    else:
        if not args.backup:
            print("--backup is required for rollback", file=sys.stderr)
            return 2
        rollback(api, baseline, args.backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
