"""Apply repeatable runtime configuration repairs for the local Open WebUI stack."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "open-webui" / "webui.db"
BACKUP_DIR = ROOT / "backups"
DATA_ANALYST_ID = "-data-analyst--developer"
WORKING_MODEL_ID = "mistralai/ministral-3-14b-reasoning"


def load_json(value: str | None) -> dict:
    return json.loads(value) if value else {}


def repair_config(config: dict) -> None:
    openai = config.setdefault("openai", {})
    openai["enable"] = True
    openai["api_base_urls"] = ["http://host.docker.internal:4321/v1"]
    openai["api_keys"] = [""]
    openai["api_configs"] = {"0": {"enable": True}}

    ollama = config.setdefault("ollama", {})
    ollama["enable"] = False
    ollama["base_urls"] = []
    ollama["api_configs"] = {}

    rag = config.setdefault("rag", {})
    web = rag.setdefault("web", {})
    search = web.setdefault("search", {})
    search.update(
        {
            "enable": True,
            "engine": "searxng",
            "result_count": 4,
            "concurrent_requests": 2,
            "searxng_query_url": "http://searxng:8080/search",
        }
    )
    loader = web.setdefault("loader", {})
    loader.update(
        {
            "engine": "external",
            "external_web_loader_url": "http://crawl4ai-proxy:8000/crawl",
            "external_web_loader_api_key": "local_bypass",
            "concurrent_requests": 2,
        }
    )

    config.setdefault("memories", {})["enable"] = True


def repair_model(meta: dict, params: dict) -> None:
    meta["toolIds"] = ["swarm_controls"]
    meta["defaultFeatureIds"] = ["code_interpreter"]
    meta.setdefault("capabilities", {})["terminal"] = False

    params["function_calling"] = "native"
    params["temperature"] = 0
    params["system"] = (
        "You are a professional Data Analyst & Developer. Use the built-in Code "
        "Interpreter whenever calculation, parsing, plotting, data transformation, "
        "or code execution is requested. Use Web Search only when current or "
        "external information is required, and cite the sources you use. When data "
        "is supplied inline in the prompt, parse it directly from a string and never "
        "invent or assume a file path. For inline CSV, strip surrounding whitespace "
        "before using csv.DictReader with io.StringIO so headers are handled "
        "correctly. A requested format such as REGION=TOTAL means print each region "
        "name and its aggregated numeric total; it is never a filter or literal field "
        "value. Verify every identifier and column name before execution. Use the "
        "minimum number of tool calls, correct an execution error at most once, and "
        "after a successful execute_code result immediately answer without calling "
        "execute_code again. Explain results clearly and verify computed outputs "
        "before answering."
    )


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"webui-before-data-analyst-repair-{stamp}.db"

    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)

    now = int(time.time())
    with sqlite3.connect(DB_PATH) as connection:
        config_row = connection.execute(
            "SELECT id, data FROM config ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not config_row:
            raise SystemExit("No Open WebUI config row found")
        config = load_json(config_row[1])
        repair_config(config)
        connection.execute(
            "UPDATE config SET data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(config), config_row[0]),
        )

        model_row = connection.execute(
            "SELECT meta, params FROM model WHERE id = ?",
            (DATA_ANALYST_ID,),
        ).fetchone()
        if not model_row:
            raise SystemExit(f"Model profile not found: {DATA_ANALYST_ID}")
        meta, params = map(load_json, model_row)
        repair_model(meta, params)
        connection.execute(
            """
            UPDATE model
            SET base_model_id = ?, name = ?, meta = ?, params = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                WORKING_MODEL_ID,
                "Data Analyst & Developer",
                json.dumps(meta),
                json.dumps(params),
                now,
                DATA_ANALYST_ID,
            ),
        )

        tool_row = connection.execute(
            "SELECT content FROM tool WHERE id = 'swarm_controls'"
        ).fetchone()
        if tool_row:
            content = tool_row[0]
            content = content.replace(
                "http://localhost:8100", "http://langgraph-orchestrator:8100"
            ).replace(
                "http://host.docker.internal:1234/v1/models",
                "http://host.docker.internal:4321/v1/models",
            )
            connection.execute(
                "UPDATE tool SET content = ?, updated_at = ? WHERE id = 'swarm_controls'",
                (content, now),
            )

        connection.commit()

    print(f"Backup: {backup_path}")
    print(f"Repaired Data Analyst profile: {WORKING_MODEL_ID}")
    print("Repaired providers, web search/loader, memory, and Swarm Controls routes")


if __name__ == "__main__":
    main()
