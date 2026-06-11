from __future__ import annotations

import time
import uuid
from collections import Counter
from typing import Any, Callable


def check(category: str, name: str, status: str, summary: str, recommendation: str = "", **evidence):
    return {
        "category": category,
        "name": name,
        "status": status,
        "summary": summary,
        "recommendation": recommendation,
        "evidence": evidence,
    }


async def run_full_debug(client_factory: Callable[[int], Any]) -> dict[str, Any]:
    started_at = time.time()
    checks = []
    r = client_factory(0)
    try:
        ping_started = time.perf_counter()
        pong = await r.ping()
        ping_ms = round((time.perf_counter() - ping_started) * 1000, 2)
        checks.append(check("Connectivity", "Redis connection", "pass" if pong else "fail", f"PING completed in {ping_ms} ms.", latency_ms=ping_ms))

        server = await r.info("server")
        memory = await r.info("memory")
        stats = await r.info("stats")
        clients = await r.info("clients")
        persistence = await r.info("persistence")
        keyspace = await r.info("keyspace")
        commandstats = await r.info("commandstats")
        modules = await r.module_list()
        raw_config = await r.config_get("*")
        config = {
            name: raw_config.get(name)
            for name in ("maxmemory", "maxmemory-policy", "appendonly", "save", "protected-mode", "bind", "requirepass", "aclfile")
        }
        acl = await r.acl_list()
        slowlog = await r.slowlog_get(20)

        checks.append(check("Runtime", "Server runtime", "pass", f"Redis {server.get('redis_version')} is running in {server.get('redis_mode')} mode.", version=server.get("redis_version"), mode=server.get("redis_mode"), uptime_seconds=server.get("uptime_in_seconds")))
        fragmentation = float(memory.get("mem_fragmentation_ratio", 0))
        memory_status = "warning" if fragmentation > 2 else "pass"
        checks.append(check("Memory", "Memory and fragmentation", memory_status, f"Redis uses {memory.get('used_memory_human')} with {fragmentation:.2f}x fragmentation.", "Review allocator/RSS usage if fragmentation remains above 2x." if memory_status == "warning" else "", used_memory=memory.get("used_memory"), used_memory_human=memory.get("used_memory_human"), used_memory_peak=memory.get("used_memory_peak"), fragmentation_ratio=fragmentation, maxmemory=memory.get("maxmemory"), maxmemory_policy=memory.get("maxmemory_policy")))
        persistence_ok = persistence.get("rdb_last_bgsave_status") == "ok" and persistence.get("aof_last_write_status", "ok") == "ok"
        checks.append(check("Persistence", "RDB and AOF status", "pass" if persistence_ok else "fail", "Persistence status is healthy." if persistence_ok else "A persistence operation reported failure.", "Review Redis persistence configuration and disk health." if not persistence_ok else "", rdb_last_bgsave_status=persistence.get("rdb_last_bgsave_status"), rdb_last_save_time=persistence.get("rdb_last_save_time"), aof_enabled=persistence.get("aof_enabled"), aof_last_write_status=persistence.get("aof_last_write_status")))
        client_status = "warning" if int(clients.get("blocked_clients", 0)) else "pass"
        checks.append(check("Clients", "Client connections", client_status, f"{clients.get('connected_clients', 0)} clients connected; {clients.get('blocked_clients', 0)} blocked.", "Investigate blocked clients." if client_status == "warning" else "", connected_clients=clients.get("connected_clients"), blocked_clients=clients.get("blocked_clients"), rejected_connections=stats.get("rejected_connections"), input_kbps=stats.get("instantaneous_input_kbps"), output_kbps=stats.get("instantaneous_output_kbps")))
        error_status = "warning" if int(stats.get("total_error_replies", 0)) or slowlog else "pass"
        top_commands = sorted(
            [{"command": name.removeprefix("cmdstat_"), "calls": data.get("calls"), "usec_per_call": data.get("usec_per_call"), "failed_calls": data.get("failed_calls")} for name, data in commandstats.items() if isinstance(data, dict)],
            key=lambda item: int(item.get("calls") or 0),
            reverse=True,
        )[:10]
        checks.append(check("Performance", "Commands, errors, and slow log", error_status, f"{stats.get('total_error_replies', 0)} error replies and {len(slowlog)} slow-log entries recorded.", "Review error-producing clients and slow commands." if error_status == "warning" else "", instantaneous_ops_per_sec=stats.get("instantaneous_ops_per_sec"), total_commands_processed=stats.get("total_commands_processed"), total_error_replies=stats.get("total_error_replies"), slowlog_count=len(slowlog), top_commands=top_commands))
        checks.append(check("Modules", "Loaded modules", "pass", f"{len(modules)} Redis modules loaded.", modules=[{"name": module.get("name"), "version": module.get("ver")} for module in modules]))

        broad_bind = config.get("bind") in {"*", "* -::*"} or "*" in str(config.get("bind", "")).split()
        no_auth = not config.get("requirepass") and any("nopass" in line for line in acl)
        security_status = "warning" if broad_bind or config.get("protected-mode") == "no" or no_auth else "pass"
        checks.append(check("Security", "Network and authentication posture", security_status, "Redis has broad network binding and/or no authentication." if security_status == "warning" else "Redis network and authentication posture is restricted.", "Keep the host port local-only and plan ACL/password plus restricted binding hardening." if security_status == "warning" else "", protected_mode=config.get("protected-mode"), bind=config.get("bind"), authentication_configured=not no_auth, acl_user_count=len(acl), aclfile_configured=bool(config.get("aclfile"))))

        database_evidence = []
        total_keys = 0
        for db in range(16):
            db_client = client_factory(db)
            type_counts: Counter[str] = Counter()
            ttl_buckets = Counter()
            memory_bytes = 0
            scanned = 0
            cursor = 0
            try:
                while True:
                    cursor, names = await db_client.scan(cursor=cursor, count=250)
                    if names:
                        pipe = db_client.pipeline(transaction=False)
                        for name in names:
                            pipe.type(name)
                            pipe.ttl(name)
                            pipe.memory_usage(name)
                        metadata = await pipe.execute()
                        for index in range(len(names)):
                            kind, ttl, size = metadata[index * 3:index * 3 + 3]
                            type_counts[str(kind)] += 1
                            memory_bytes += int(size or 0)
                            ttl_buckets["persistent" if int(ttl) < 0 else "expiring"] += 1
                        scanned += len(names)
                    if int(cursor) == 0:
                        break
            finally:
                await db_client.aclose()
            total_keys += scanned
            database_evidence.append({"db": db, "keys": scanned, "types": dict(type_counts), "ttl": dict(ttl_buckets), "memory_bytes": memory_bytes})
        checks.append(check("Keyspace", "Metadata-only keyspace inventory", "pass", f"Scanned metadata for {total_keys} keys without retaining names or values.", databases=database_evidence, total_keys=total_keys))
    except Exception as exc:
        checks.append(check("Connectivity", "Full debug execution", "fail", f"{type(exc).__name__}: {str(exc)[:300]}", "Restore Redis connectivity and rerun Full Debug."))
    finally:
        await r.aclose()

    counts = Counter(item["status"] for item in checks)
    status = "fail" if counts["fail"] else ("warning" if counts["warning"] else "pass")
    return {
        "run_id": str(uuid.uuid4()),
        "status": status,
        "started_at": started_at,
        "completed_at": time.time(),
        "summary": {"pass": counts["pass"], "warning": counts["warning"], "fail": counts["fail"], "total": len(checks)},
        "checks": checks,
    }
