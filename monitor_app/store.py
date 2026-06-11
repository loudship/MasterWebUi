from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


SENSITIVE_KEY = re.compile(
    r"(authorization|api[-_]?key|token|secret|password|cookie)", re.IGNORECASE
)
MAX_ARTIFACT_CHARS = 100_000


def sanitize_artifact(value: Any, max_chars: int = MAX_ARTIFACT_CHARS) -> Any:
    def redact(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child) for child in item]
        if isinstance(item, tuple):
            return [redact(child) for child in item]
        return item

    cleaned = redact(value)
    encoded = json.dumps(cleaned, ensure_ascii=True, default=str)
    if len(encoded) <= max_chars:
        return cleaned
    return {
        "truncated": True,
        "original_chars": len(encoded),
        "preview": encoded[:max_chars],
    }


class RunStore:
    def __init__(self, path: str, retention_days: int = 30, max_runs: int = 500):
        self.path = Path(path)
        self.retention_days = max(1, retention_days)
        self.max_runs = max(1, max_runs)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS diagnostic_runs (
                    run_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    request_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS diagnostic_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    detail TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES diagnostic_runs(run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_runs_started ON diagnostic_runs(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_steps_run ON diagnostic_steps(run_id, id);
                """
            )

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(sanitize_artifact(value), ensure_ascii=True, default=str)

    @staticmethod
    def _load(value: str) -> Any:
        return json.loads(value)

    def create_run(self, run_id: str, kind: str, request: dict[str, Any]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO diagnostic_runs
                (run_id, kind, status, started_at, request_json, summary_json)
                VALUES (?, ?, 'running', ?, ?, '{}')
                """,
                (run_id, kind, time.time(), self._dump(request)),
            )
        self.prune()

    def add_step(
        self,
        run_id: str,
        name: str,
        label: str,
        status: str,
        detail: str,
        evidence: Any,
        started_at: float,
        completed_at: float | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO diagnostic_steps
                (run_id, name, label, status, started_at, completed_at, detail, evidence_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    name,
                    label,
                    status,
                    started_at,
                    completed_at or time.time(),
                    detail[:2000],
                    self._dump(evidence),
                ),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        summary: dict[str, Any],
        error: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE diagnostic_runs
                SET status = ?, completed_at = ?, summary_json = ?, error = ?
                WHERE run_id = ?
                """,
                (status, time.time(), self._dump(summary), error, run_id),
            )
        self.prune()

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, kind, status, started_at, completed_at, summary_json, error
                FROM diagnostic_runs ORDER BY started_at DESC LIMIT ?
                """,
                (min(max(limit, 1), 500),),
            ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result["summary"] = self._load(result.pop("summary_json"))
            results.append(result)
        return results

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM diagnostic_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not run:
                return None
            steps = connection.execute(
                "SELECT * FROM diagnostic_steps WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        result = dict(run)
        result["request"] = self._load(result.pop("request_json"))
        result["summary"] = self._load(result.pop("summary_json"))
        result["steps"] = [
            {
                **dict(step),
                "evidence": self._load(step["evidence_json"]),
            }
            for step in steps
        ]
        for step in result["steps"]:
            step.pop("evidence_json", None)
        return result

    def prune(self) -> None:
        cutoff = time.time() - (self.retention_days * 86400)
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM diagnostic_runs WHERE started_at < ?", (cutoff,))
            stale = connection.execute(
                """
                SELECT run_id FROM diagnostic_runs
                ORDER BY started_at DESC LIMIT -1 OFFSET ?
                """,
                (self.max_runs,),
            ).fetchall()
            if stale:
                connection.executemany(
                    "DELETE FROM diagnostic_runs WHERE run_id = ?",
                    [(row["run_id"],) for row in stale],
                )
            connection.execute(
                "DELETE FROM diagnostic_steps WHERE run_id NOT IN (SELECT run_id FROM diagnostic_runs)"
            )


def run_markdown(run: dict[str, Any]) -> str:
    lines = [
        f"# Web Tools Control Center Run {run['run_id']}",
        "",
        f"- Status: **{run['status'].upper()}**",
        f"- Kind: `{run['kind']}`",
        f"- Started: `{run['started_at']}`",
        f"- Completed: `{run.get('completed_at')}`",
        "",
        "## Steps",
        "",
    ]
    for step in run["steps"]:
        lines.extend(
            [
                f"### {step['label']}",
                "",
                f"- Status: **{step['status'].upper()}**",
                f"- Detail: {step['detail']}",
                "",
                "```json",
                json.dumps(step["evidence"], indent=2, ensure_ascii=True, default=str),
                "```",
                "",
            ]
        )
    return "\n".join(lines)
