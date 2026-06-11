from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .baseline import BaselineLoader
from .diff_engine import build_diffs
from .security import chat_label, fingerprint, sanitize, user_label


log = logging.getLogger("config-drift-monitor")


ADMIN_ENDPOINTS = {
    "public_config": "/api/config",
    "config_export": "/api/v1/configs/export",
    "auth_admin": "/api/v1/auths/admin/config",
    "retrieval": "/api/v1/retrieval/config",
    "audio": "/api/v1/audio/config",
    "tasks": "/api/v1/tasks/config",
    "openai": "/openai/config",
    "images": "/api/v1/images/config",
    "connections": "/api/v1/configs/connections",
    "tool_servers": "/api/v1/configs/tool_servers",
    "code_execution": "/api/v1/configs/code_execution",
    "models_config": "/api/v1/configs/models",
}


class ReadOnlyOpenWebUIClient:
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 15):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            trust_env=False,
        )

    async def get(self, path: str) -> Any:
        response = await self.client.get(path)
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self.client.aclose()


class DriftEngine:
    def __init__(
        self,
        client: ReadOnlyOpenWebUIClient,
        baseline: BaselineLoader,
        admin_interval: int = 30,
        workspace_interval: int = 30,
        user_interval: int = 60,
        chat_interval: int = 60,
        recent_chat_hours: int = 24,
        chats_per_user: int = 20,
        max_chats: int = 100,
    ):
        self.client = client
        self.baseline_loader = baseline
        self.intervals = {"admin": admin_interval, "workspace": workspace_interval, "user": user_interval, "chat": chat_interval}
        self.recent_chat_hours = recent_chat_hours
        self.chats_per_user = chats_per_user
        self.max_chats = max_chats
        self.planes = {name: self._empty_plane(name) for name in ("admin", "workspace", "user", "chat")}
        self.diffs: list[dict[str, Any]] = []
        self.generated_at: float | None = None
        self.event_version = 0
        self.baseline_error = ""
        self._refresh_lock = asyncio.Lock()
        self._plane_locks = {name: asyncio.Lock() for name in self.planes}
        self._last_manual_refresh = 0.0
        self._tasks: list[asyncio.Task] = []
        self._raw_user_refs: list[dict[str, str]] = []

    @staticmethod
    def _empty_plane(name: str) -> dict[str, Any]:
        return {"name": name, "status": "unavailable", "observed_at": None, "latency_ms": 0, "item_count": 0, "error": "Not polled yet.", "data": {}}

    async def start(self) -> None:
        await self.refresh_all()
        self._tasks = [asyncio.create_task(self._poll_loop(name), name=f"drift-{name}-poller") for name in self.planes]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.close()

    async def _poll_loop(self, plane: str) -> None:
        while True:
            await asyncio.sleep(self.intervals[plane])
            await self.poll_plane(plane)

    async def refresh_all(self, manual: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if manual and now - self._last_manual_refresh < 5:
            raise RuntimeError("Manual refresh is limited to once every five seconds.")
        async with self._refresh_lock:
            if manual:
                self._last_manual_refresh = time.monotonic()
            await asyncio.gather(self.poll_admin(), self.poll_workspace(), self.poll_users())
            await self.poll_chats()
            self._recompute()
            return self.snapshot()

    async def poll_plane(self, plane: str) -> None:
        if plane == "admin":
            await self.poll_admin()
        elif plane == "workspace":
            await self.poll_workspace()
        elif plane == "user":
            await self.poll_users()
        elif plane == "chat":
            await self.poll_chats()
        self._recompute()

    async def poll_admin(self) -> None:
        async with self._plane_locks["admin"]:
            started = time.perf_counter()
            data, errors = {}, []
            async def fetch(name: str, path: str) -> None:
                try:
                    data[name] = sanitize(await self.client.get(path), name)
                except Exception as exc:
                    errors.append(f"{name}: {self._error(exc)}")
            await asyncio.gather(*(fetch(name, path) for name, path in ADMIN_ENDPOINTS.items()))
            self.planes["admin"] = self._plane_result("admin", data, errors, started, len(data))

    async def poll_workspace(self) -> None:
        async with self._plane_locks["workspace"]:
            started = time.perf_counter()
            try:
                exported, listed, base = await asyncio.gather(
                    self.client.get("/api/v1/models/export"),
                    self.client.get("/api/v1/models/list"),
                    self.client.get("/api/v1/models/base"),
                )
                models = []
                for model in exported:
                    models.append(
                        {
                            "id": model.get("id"),
                            "entity_label": f"Model · {model.get('id')}",
                            "params": sanitize(model.get("params") or {}, "params"),
                            "meta": sanitize(model.get("meta") or {}, "meta"),
                            "base_model_id": model.get("base_model_id"),
                        }
                    )
                data = {"models": models, "list_count": (listed or {}).get("total", len(models)), "base_model_ids": [item.get("id") for item in base]}
                self.planes["workspace"] = self._plane_result("workspace", data, [], started, len(models))
            except Exception as exc:
                self.planes["workspace"] = self._plane_result("workspace", {}, [self._error(exc)], started, 0)

    async def poll_users(self) -> None:
        async with self._plane_locks["user"]:
            started = time.perf_counter()
            try:
                users_payload, permissions = await asyncio.gather(
                    self._fetch_all_users(),
                    self.client.get("/api/v1/users/default/permissions"),
                )
                users, refs = [], []
                for user in users_payload.get("users", []):
                    label = user_label(user)
                    users.append(
                        {
                            "id_suffix": str(user.get("id", ""))[-8:],
                            "entity_label": label,
                            "role": user.get("role"),
                            "settings": sanitize(user.get("settings") or {}, "settings"),
                            "last_active_at": user.get("last_active_at"),
                        }
                    )
                    refs.append({"id": str(user.get("id")), "entity_label": label})
                self._raw_user_refs = refs
                data = {"users": users, "default_permissions": sanitize(permissions, "permissions"), "total": users_payload.get("total", len(users))}
                self.planes["user"] = self._plane_result("user", data, [], started, len(users))
            except Exception as exc:
                self._raw_user_refs = []
                self.planes["user"] = self._plane_result("user", {}, [self._error(exc)], started, 0)

    async def _fetch_all_users(self) -> dict[str, Any]:
        payload = await self.client.get("/api/v1/users/")
        users = list(payload.get("users", []))
        total = int(payload.get("total", len(users)) or len(users))
        seen = {str(user.get("id")) for user in users}
        page = 2
        while len(users) < total and page <= 200:
            page_payload = await self.client.get(f"/api/v1/users/?page={page}")
            page_users = page_payload.get("users", [])
            new_users = [user for user in page_users if str(user.get("id")) not in seen]
            if not new_users:
                break
            users.extend(new_users)
            seen.update(str(user.get("id")) for user in new_users)
            page += 1
        return {"users": users, "total": total}

    async def poll_chats(self) -> None:
        async with self._plane_locks["chat"]:
            started = time.perf_counter()
            cutoff = time.time() - self.recent_chat_hours * 3600
            semaphore = asyncio.Semaphore(2)
            summaries: list[tuple[dict[str, str], dict[str, Any]]] = []
            errors: list[str] = []
            for user in self._raw_user_refs:
                try:
                    payload = await self.client.get(f"/api/v1/chats/list/user/{user['id']}?page=1&order_by=updated_at&direction=desc")
                    recent = [item for item in payload if float(item.get("updated_at") or 0) >= cutoff][: self.chats_per_user]
                    summaries.extend((user, item) for item in recent)
                except Exception as exc:
                    errors.append(f"{user['entity_label']}: {self._error(exc)}")
            summaries = sorted(summaries, key=lambda item: item[1].get("updated_at") or 0, reverse=True)[: self.max_chats]

            async def fetch(user: dict[str, str], summary: dict[str, Any]) -> dict[str, Any] | None:
                async with semaphore:
                    try:
                        detail = await self.client.get(f"/api/v1/chats/{summary['id']}")
                        blob = detail.get("chat") or {}
                        system = blob.get("system")
                        return {
                            "id_suffix": str(summary.get("id", ""))[-8:],
                            "entity_label": f"{user['entity_label']} / {chat_label(str(summary.get('id')), summary.get('updated_at'))}",
                            "updated_at": summary.get("updated_at"),
                            "models": sanitize(blob.get("models") or [], "models"),
                            "params": sanitize(blob.get("params") or {}, "params"),
                            "system": fingerprint(system) if isinstance(system, str) and system else None,
                        }
                    except Exception as exc:
                        errors.append(f"chat {str(summary.get('id'))[-8:]}: {self._error(exc)}")
                        return None

            chats = [item for item in await asyncio.gather(*(fetch(user, summary) for user, summary in summaries)) if item]
            self.planes["chat"] = self._plane_result("chat", {"chats": chats, "coverage_hours": self.recent_chat_hours}, errors, started, len(chats))

    def _plane_result(self, name: str, data: dict[str, Any], errors: list[str], started: float, count: int) -> dict[str, Any]:
        return {
            "name": name,
            "status": "available" if data else "unavailable",
            "observed_at": time.time(),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "item_count": count,
            "error": "; ".join(errors)[:1000],
            "data": data,
        }

    def _recompute(self) -> None:
        try:
            baseline = self.baseline_loader.load()
            self.baseline_error = ""
            self.generated_at = time.time()
            self.diffs = build_diffs(baseline, self.planes, self.generated_at)
        except Exception as exc:
            self.baseline_error = str(exc)
            self.generated_at = time.time()
            self.diffs = []
        self.event_version += 1

    def overview(self) -> dict[str, Any]:
        counts = {status: 0 for status in ("aligned", "override", "drift", "unavailable", "unobservable", "ignored")}
        current_diffs = self.current_diffs()
        for item in current_diffs:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        fail = bool(self.baseline_error) or any(
            (item["status"] == "unavailable" and not item["detail"].startswith("Plane is stale"))
            or (item["status"] == "drift" and item["severity"] == "critical" and item["enforced"])
            for item in current_diffs
        )
        plane_summaries = []
        stale = False
        for name, plane in self.planes.items():
            summary = {key: value for key, value in plane.items() if key != "data"}
            observed_at = summary.get("observed_at")
            if summary["status"] == "available" and observed_at and time.time() - observed_at > self.intervals[name] * 2.5:
                summary["status"] = "stale"
                summary["error"] = "Plane has not refreshed within its expected observation window."
                stale = True
            plane_summaries.append(summary)
        warning = stale or any(item["status"] in {"override", "drift", "unobservable"} for item in current_diffs)
        status = "fail" if fail else ("warning" if warning else "aligned")
        baseline = {}
        try:
            loaded = self.baseline_loader.load()
            baseline = {"schema_version": loaded.get("schema_version"), "metadata": sanitize(loaded.get("metadata", {})), "rule_count": len(loaded.get("rules", [])), "valid": True, "error": ""}
        except Exception as exc:
            baseline = {"schema_version": None, "metadata": {}, "rule_count": 0, "valid": False, "error": str(exc)}
        return {
            "status": status,
            "counts": counts,
            "planes": plane_summaries,
            "baseline": baseline,
            "generated_at": self.generated_at or 0,
            "event_version": self.event_version,
        }

    def snapshot(self) -> dict[str, Any]:
        return {"overview": self.overview(), "planes": sanitize(self.effective_planes()), "diffs": self.current_diffs()}

    def effective_planes(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        effective = {}
        for name, plane in self.planes.items():
            observed_at = plane.get("observed_at")
            if plane.get("status") == "available" and observed_at and now - observed_at > self.intervals[name] * 2.5:
                effective[name] = {
                    **plane,
                    "status": "stale",
                    "data": {},
                    "error": "Plane is stale; prior values are not treated as current truth.",
                }
            else:
                effective[name] = plane
        return effective

    def current_diffs(self) -> list[dict[str, Any]]:
        try:
            return build_diffs(self.baseline_loader.load(), self.effective_planes(), self.generated_at or time.time())
        except Exception:
            return []

    def baseline_public(self) -> dict[str, Any]:
        baseline = self.baseline_loader.load()
        return sanitize(baseline)

    @staticmethod
    def _error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            return f"HTTP {exc.response.status_code}"
        return f"{type(exc).__name__}: {str(exc)[:240]}"
