"""Local-only Redis cache management and visual debugging interface."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Literal

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


REDIS_URL = os.getenv("REDIS_CACHE_URL", "redis://redis-cache:6379/0")
MAX_VALUE_BYTES = int(os.getenv("REDIS_UI_MAX_VALUE_BYTES", "1048576"))
MAX_SCAN_COUNT = 250
MAX_COLLECTION_ITEMS = 500
UI_PATH = Path(__file__).with_name("redis_ui.html")

app = FastAPI(title="Redis Cache Console", version="1.0.0")


def client(db: int = 0) -> redis.Redis:
    if not 0 <= db <= 15:
        raise HTTPException(status_code=400, detail="Database must be between 0 and 15.")
    return redis.Redis.from_url(REDIS_URL, db=db, decode_responses=True)


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(clean(k)): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def validate_key(key: str) -> str:
    if not key or len(key.encode()) > 1024:
        raise HTTPException(status_code=400, detail="Key must be between 1 and 1024 bytes.")
    return key


class KeyWrite(BaseModel):
    db: int = Field(0, ge=0, le=15)
    key: str
    type: Literal["string", "json", "hash", "list", "set", "zset"] = "string"
    value: Any
    ttl_seconds: int | None = Field(None, ge=1, le=31_536_000)


class DeleteRequest(BaseModel):
    db: int = Field(0, ge=0, le=15)
    key: str
    confirmation: str


class TtlRequest(BaseModel):
    db: int = Field(0, ge=0, le=15)
    key: str
    ttl_seconds: int | None = Field(None, ge=1, le=31_536_000)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(UI_PATH, media_type="text/html")


@app.get("/api/health")
async def health():
    r = client()
    try:
        started = time.perf_counter()
        pong = await r.ping()
        return {"status": "ok" if pong else "error", "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    finally:
        await r.aclose()


@app.get("/api/overview")
async def overview():
    r = client()
    try:
        started = time.perf_counter()
        server = await r.info("server")
        memory = await r.info("memory")
        stats = await r.info("stats")
        clients = await r.info("clients")
        persistence = await r.info("persistence")
        keyspace = await r.info("keyspace")
        commandstats = await r.info("commandstats")
        modules = await r.module_list()
        client_list = await r.client_list()
        slowlog = await r.slowlog_get(20)
        config = await r.config_get("*")
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        dbs = []
        total_keys = 0
        for db in range(16):
            data = keyspace.get(f"db{db}", {})
            keys = int(data.get("keys", 0)) if isinstance(data, dict) else 0
            total_keys += keys
            dbs.append({"db": db, "keys": keys, "expires": int(data.get("expires", 0)) if isinstance(data, dict) else 0})
        top_commands = sorted(
            (
                {"command": name.removeprefix("cmdstat_"), **details}
                for name, details in commandstats.items()
                if isinstance(details, dict)
            ),
            key=lambda item: int(item.get("calls", 0)),
            reverse=True,
        )[:12]
        config = {
            name: config.get(name)
            for name in ("maxmemory", "maxmemory-policy", "appendonly", "save", "databases", "protected-mode", "bind")
        }
        return clean(
            {
                "timestamp": time.time(),
                "latency_ms": latency_ms,
                "total_keys": total_keys,
                "server": server,
                "memory": memory,
                "stats": stats,
                "clients": clients,
                "persistence": persistence,
                "databases": dbs,
                "commands": top_commands,
                "modules": modules,
                "client_list": client_list,
                "slowlog": slowlog,
                "config": config,
            }
        )
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {exc}") from exc
    finally:
        await r.aclose()


@app.get("/api/keys")
async def keys(
    db: int = Query(0, ge=0, le=15),
    pattern: str = Query("*", max_length=256),
    cursor: int = Query(0, ge=0),
    count: int = Query(100, ge=10, le=MAX_SCAN_COUNT),
):
    r = client(db)
    try:
        next_cursor, names = await r.scan(cursor=cursor, match=pattern or "*", count=count)
        pipe = r.pipeline(transaction=False)
        for name in names:
            pipe.type(name)
            pipe.ttl(name)
            pipe.memory_usage(name)
            pipe.object("encoding", name)
        metadata = await pipe.execute()
        records = []
        for index, name in enumerate(names):
            kind, ttl, size, encoding = metadata[index * 4 : index * 4 + 4]
            records.append({"key": name, "type": kind, "ttl": ttl, "bytes": size or 0, "encoding": encoding})
        return {"db": db, "cursor": int(next_cursor), "count": len(records), "keys": clean(records)}
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await r.aclose()


@app.get("/api/key")
async def key_detail(db: int = Query(0, ge=0, le=15), key: str = Query(..., max_length=1024)):
    key = validate_key(key)
    r = client(db)
    try:
        kind = await r.type(key)
        if kind == "none":
            raise HTTPException(status_code=404, detail="Key not found.")
        ttl, size, encoding = await r.ttl(key), await r.memory_usage(key), await r.object("encoding", key)
        truncated = False
        if kind == "string":
            value = await r.get(key)
            if value and len(value.encode()) > MAX_VALUE_BYTES:
                value, truncated = value[:MAX_VALUE_BYTES], True
        elif kind == "hash":
            value = await r.hgetall(key)
            truncated = len(value) > MAX_COLLECTION_ITEMS
            value = dict(list(value.items())[:MAX_COLLECTION_ITEMS])
        elif kind == "list":
            length = await r.llen(key)
            value, truncated = await r.lrange(key, 0, MAX_COLLECTION_ITEMS - 1), length > MAX_COLLECTION_ITEMS
        elif kind == "set":
            _, value = await r.sscan(key, cursor=0, count=MAX_COLLECTION_ITEMS)
            value = list(value)[:MAX_COLLECTION_ITEMS]
            truncated = await r.scard(key) > MAX_COLLECTION_ITEMS
        elif kind == "zset":
            value = await r.zrange(key, 0, MAX_COLLECTION_ITEMS - 1, withscores=True)
            truncated = await r.zcard(key) > MAX_COLLECTION_ITEMS
        elif kind == "ReJSON-RL":
            value = json.loads(await r.execute_command("JSON.GET", key))
        elif kind == "stream":
            value = await r.xrevrange(key, count=MAX_COLLECTION_ITEMS)
            truncated = await r.xlen(key) > MAX_COLLECTION_ITEMS
        else:
            value = f"Preview is not available for Redis type {kind}."
        return clean({"db": db, "key": key, "type": kind, "ttl": ttl, "bytes": size or 0, "encoding": encoding, "truncated": truncated, "value": value})
    finally:
        await r.aclose()


@app.post("/api/key")
async def write_key(request: KeyWrite):
    key = validate_key(request.key)
    encoded = json.dumps(request.value, ensure_ascii=True).encode()
    if len(encoded) > MAX_VALUE_BYTES:
        raise HTTPException(status_code=413, detail="Value exceeds the 1 MiB UI write limit.")
    r = client(request.db)
    try:
        async with r.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            if request.type == "string":
                pipe.set(key, request.value if isinstance(request.value, str) else json.dumps(request.value))
            elif request.type == "json":
                pipe.execute_command("JSON.SET", key, "$", json.dumps(request.value))
            elif request.type == "hash":
                if not isinstance(request.value, dict) or not request.value:
                    raise HTTPException(status_code=400, detail="Hash value must be a non-empty object.")
                pipe.hset(key, mapping={str(k): str(v) for k, v in request.value.items()})
            elif request.type == "list":
                if not isinstance(request.value, list) or not request.value:
                    raise HTTPException(status_code=400, detail="List value must be a non-empty array.")
                pipe.rpush(key, *[str(item) for item in request.value])
            elif request.type == "set":
                if not isinstance(request.value, list) or not request.value:
                    raise HTTPException(status_code=400, detail="Set value must be a non-empty array.")
                pipe.sadd(key, *[str(item) for item in request.value])
            elif request.type == "zset":
                if not isinstance(request.value, dict) or not request.value:
                    raise HTTPException(status_code=400, detail="Sorted-set value must be a non-empty object of member: score.")
                pipe.zadd(key, {str(k): float(v) for k, v in request.value.items()})
            if request.ttl_seconds:
                pipe.expire(key, request.ttl_seconds)
            await pipe.execute()
        return {"status": "saved", "db": request.db, "key": key, "type": request.type}
    except (redis.RedisError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await r.aclose()


@app.post("/api/key/ttl")
async def update_ttl(request: TtlRequest):
    key = validate_key(request.key)
    r = client(request.db)
    try:
        exists = await r.exists(key)
        if not exists:
            raise HTTPException(status_code=404, detail="Key not found.")
        result = await (r.persist(key) if request.ttl_seconds is None else r.expire(key, request.ttl_seconds))
        return {"status": "updated", "changed": bool(result), "ttl": await r.ttl(key)}
    finally:
        await r.aclose()


@app.delete("/api/key")
async def delete_key(request: DeleteRequest):
    key = validate_key(request.key)
    if request.confirmation != key:
        raise HTTPException(status_code=400, detail="Confirmation must exactly match the key name.")
    r = client(request.db)
    try:
        deleted = await r.delete(key)
        return {"status": "deleted" if deleted else "not_found", "deleted": bool(deleted)}
    finally:
        await r.aclose()
