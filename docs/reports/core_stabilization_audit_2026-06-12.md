# Core Stabilization Audit — 2026-06-12

> **Implementation status (same day):** all 🔴/🟠 findings and the 🟡/⚪ hygiene
> items below are FIXED and covered by tests (`pytest -k "not container"`:
> 250 passed). Resolutions that differ from the original recommendation:
>
> - **P3-4 (SYS_ADMIN/cgroup rw on open-webui): working as designed.** The
>   gVisor code-execution sandbox blueprint (§5.1) requires exactly these
>   privileges and `tests/test_workspace_environment_repairs.py` pins them as
>   the minimal set. Left in place; now also pinned in
>   `tests/test_compose_stabilization.py` so removal is a conscious decision.
> - **WEB_SEARCH_CONCURRENT_REQUESTS=1: left as-is.** It is a deliberate
>   "tight defaults" policy pinned across `master_webui_config.yaml`, compose,
>   and the drift baseline (`tests/test_web_search_defaults.py`).
> - **New finding fixed during implementation:** `hitl_broker.py` set a global
>   `socket_timeout=5`, which aborted every BLPOP read after 5 s — HITL
>   authorizations auto-denied long before the 120 s approval window.
> - **P2-4 (true token-level streaming through the LangGraph nodes) is the one
>   item NOT implemented** — it requires re-architecting `_llm_call` into a
>   streaming consumer with custom LangGraph event dispatch and cannot be
>   verified on this host (no langgraph runtime). The /stream endpoint now has
>   interruptible task semantics and a bounded relay queue as groundwork.

Scope: custom code layer of the hardened air-gapped stack (inference-gateway, pipelines,
langgraph-orchestrator, telemetry-gateway, deep-web-mcp, workspace catalog tools, VRAM
arbiter, frontend override layer, docker-compose). The vendored Open WebUI 0.9.6 tree was
treated as upstream and only inspected to verify API contracts and UI behavior.

Note on framing: the frontend is **Svelte** (Open WebUI 0.9.6), not React. Phase 3 findings
apply to the Svelte app plus the `patch_frontend.mjs` override layer.

Severity: 🔴 critical (feature dead or systemwide hang) · 🟠 high · 🟡 medium · ⚪ low

---

## Phase 1 — Hardware & Inference

### 🔴 P1-1 VRAM "eviction" loads the model instead of unloading it
- **File:** `vram_arbiter_daemon.py:194-198`
- **Root cause:** On a ceiling breach the daemon executes
  `lms load <model> --ttl 300`. `lms load` *loads* a model (optionally with an idle TTL);
  it never frees VRAM at breach time. Best case the model idles out 5 minutes later; worst
  case the command spawns a second instance and *increases* pressure.
- **Fix:**
  ```python
  proc = await asyncio.create_subprocess_exec(
      self._lms_bin, "unload", model_key,
      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
  )
  ```
  Keep the cooldown guard; log reclaimed state by re-polling after the unload returns.

### 🔴 P1-2 The arbiter cannot run where it is packaged to run
- **Files:** `Dockerfile.arbiter` (ENTRYPOINT `vram_arbiter_daemon.py`),
  `vram_arbiter_daemon.py:117-140`, `docker-compose.yml` (no service entry)
- **Root cause:** Three compounding problems:
  1. The daemon is packaged into a **Linux** container but `_find_lms_binary()` resolves
     Windows paths (`LOCALAPPDATA…lms.exe`, `USERPROFILE…lms.exe`). Inside the container
     every eviction ends in `FileNotFoundError` — the safeguard is structurally inert.
  2. The arbiter service is not declared in any compose file, so nothing supervises it.
  3. The container cannot invoke a host CLI at all; CLI-based eviction only works if the
     daemon runs **on the Windows host**.
- **Fix (pick one, both are offline):**
  - Run `vram_arbiter_daemon.py` as a host-side scheduled task / NSSM service (it already
    has no container dependencies), or
  - Keep it containerized and replace the CLI with LM Studio's local REST unload call
    (verify the exact route against the installed LM Studio version — the legacy
    `vram_arbiter.py` posts `/v1/models/unload` with `instance_id`, which is not part of
    the OpenAI-compatible surface and almost certainly 404s).
  Then add it to `docker-compose.yml` (or a host supervisor) so it actually runs.

### 🟠 P1-3 VRAM accounting is a constant, not a measurement
- **Files:** `vram_arbiter_daemon.py:84-86, 242-248`; `Dockerfile.arbiter:54`
- **Root cause:** "VRAM usage" = `len(models) × VRAM_PER_MODEL_ESTIMATE` (5.7 GiB in code,
  5 GiB in the image — the two defaults disagree). A single oversized model or a long
  KV-cache can OOM the 12 GB RTX 5070 with the arbiter reading 47% usage. Additionally,
  `_extract_loaded_models()` expects `{"models":[{"key","loaded_instances"}]}`, which does
  not match LM Studio's documented `/api/v0/models` schema (`{"data":[{"id","state"}]}`);
  the fallback path then counts *downloaded* models as *loaded*, causing false breaches.
- **Fix:** Measure, don't estimate. On the host: `pynvml`
  (`nvmlDeviceGetMemoryInfo(handle).used`) with
  `VRAM_CEILING = total − UI_RESERVE` (reserve ≥ 1.5 GiB for compositor/browser on a
  12 GB card). Parse the version-correct LM Studio endpoint and filter on
  `state == "loaded"`. Trip eviction on measured bytes, not model count.

### 🟠 P1-4 `desktop_eye.py` competes with chat for the GPU every 10 seconds
- **File:** `desktop_eye.py:9-12, 77, 100-102`
- **Root cause:** A synchronous loop screenshots the desktop and pushes a VLM inference
  to `localhost:4321` every 10 s — *directly to LM Studio*, bypassing the
  inference-gateway allowlist and its serialization lock. This keeps `qwen2-vl-4b`
  resident in VRAM permanently and interleaves vision prefill with interactive token
  generation: periodic stutter on every chat stream and ~4 GB of the 12 GB budget gone.
- **Fix:** Route through the gateway (`http://127.0.0.1:4322/v1/chat/completions`) so the
  allowlist and the GPU mutex apply; lengthen the interval or make capture event-driven;
  skip the cycle when the gateway reports an in-flight request (one cheap probe of
  `inference_gateway_metrics` or a `/busy` endpoint).

### 🟠 P1-5 Ingestion "parallelism" is aimed at the wrong bottleneck and the API call is wrong
- **File:** `workspace/catalog-tools/docling_ingestion.py:43, 110-115, 316-348`
- **Root cause (two layered defects):**
  1. **Contract mismatch — every chunk fails.** `_embed_single_chunk` POSTs
     `{name, content, meta}` to `/api/v1/knowledge/{id}/file/add`, but that endpoint
     (vendored `routers/knowledge.py:699`) takes `KnowledgeFileIdForm` — the **file_id of
     an already-uploaded file**. Every request 400s; ingestion reports `embedded 0/N`.
  2. **Misdirected concurrency.** `Semaphore(32)` ("host thread count") fans 32 concurrent
     HTTP calls into Open WebUI, which runs with `UVICORN_WORKERS: "1"`. Coroutines are
     not threads; the fan-out doesn't use the 9950X — it floods the single event loop that
     also serves the UI, which is precisely the "background work starves the chat" failure
     mode. Embedding compute happens inside Open WebUI's process either way.
- **Fix:** Two-step contract: `POST /api/v1/files/` (multipart, one file per logical
  document — not per chunk; let Open WebUI's splitter chunk it), then
  `POST /api/v1/knowledge/{id}/file/add` with `{"file_id": ...}`. Cap concurrency at 3-4.
  For true CPU parallelism move embedding out of the UI process (dedicated worker
  container draining the queue below).

### 🟡 P1-6 Write-only Redis queue (resource leak, dead architecture)
- **File:** `workspace/catalog-tools/docling_ingestion.py:44-46, 266-295`
- **Root cause:** Chunks are LPUSH'd to `ingestion:queue`, then the same chunks are
  embedded directly in-process. Nothing ever BRPOPs the queue (`REDIS_QUEUE_TIMEOUT_S` is
  unused). Entries accumulate until the 500-item cap, after which pushes are silently
  skipped forever — a leak followed by dead code.
- **Fix:** Either delete the queue path, or make it real: tool only enqueues; a separate
  worker service (compose `profiles: [offline-tools]`) BRPOPs and performs upload+attach
  at controlled concurrency. The second option is the correct Ryzen-class design — it
  decouples ingestion from the chat event loop entirely.

### 🟡 P1-7 Sanitizer corrupts ingested documents
- **File:** `workspace/catalog-tools/docling_ingestion.py:75-79, 364-374`
- **Root cause:** `_SQL_TOKENS` rewrites `'`, `--`, `/*` and words like `DELETE FROM` to
  `[REDACTED]` inside *document prose*. Every English contraction becomes
  `don[REDACTED]t`; technical PDFs about SQL are destroyed. SQL-injection scrubbing of
  vector-store *content* is security theater — the DB layer uses parameterized queries.
- **Fix:** Drop `_SQL_TOKENS` entirely; keep NFC normalization, control-char stripping
  (NUL removal is the only thing PostgreSQL actually requires), and whitespace collapse.

### 🟡 P1-8 Global GPU mutex also serializes embeddings and model listing
- **File:** `services/inference-gateway/inference_gateway.py:35, 97, 137, 235`
- **Root cause:** One `asyncio.Lock` covers chat, `/v1/embeddings`, and `GET /v1/models`.
  Serializing *generation* on a single 12 GB GPU is correct; serializing a metadata GET
  and small embedding batches behind a 3-minute generation is not — it is why the model
  selector and RAG indexing freeze during streams (see P2-2/P2-3).
- **Fix:** Lock generation only. Serve `/v1/models` from a 5 s in-memory cache refreshed
  outside the lock; give embeddings their own semaphore (CPU/GPU cost is small and
  LM Studio handles them concurrently).

---

## Phase 2 — Backend ↔ Frontend Communication

### 🔴 P2-1 The pipelines service is a healthcheck stub — the primary model path is dead
- **Files:** `services/pipelines/app.py` (entire file), `services/pipelines/Dockerfile`,
  `docker-compose.yml:199-228, 236-239`
- **Root cause:** The container Open WebUI is pointed at
  (`OPENAI_API_BASE_URL=http://pipelines:9099/v1`) serves exactly two routes: `/` and
  `/health`. It never installs the Open WebUI Pipelines runtime, so the
  `./pipelines/langgraph_router.py` volume mount is loaded by nothing, `/v1/models`
  returns 404, and no pipeline model can appear in the UI. The compose healthcheck probes
  `/` — so the dead service reports **healthy** and every dependent service starts green.
- **Fix:** Build the real runtime offline (vendor `ghcr.io/open-webui/pipelines:main` by
  digest, `pull_policy: never`, mount `./pipelines` as `/app/pipelines`,
  `PIPELINES_API_KEY` already wired), or implement a genuine minimal manifold server
  exposing `/v1/models` + `/v1/chat/completions` that imports `langgraph_router.Pipeline`.
  Change the healthcheck to assert `GET /v1/models` returns the manifold — health must
  measure the contract, not the process.

### 🔴 P2-2 Streams hold the global GPU lock with no read timeout → one stall freezes everything
- **File:** `services/inference-gateway/inference_gateway.py:137-150`
  (`timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)`)
- **Root cause:** `_proxy_stream` acquires `_inference_lock` inside the generator and
  holds it for the stream's lifetime with **no `sock_read` timeout**. If LM Studio stalls
  mid-generation without closing the socket (driver hiccup, VRAM thrash — exactly the
  high-load case this stack must survive), the lock is held forever. Every subsequent
  chat, embedding, and model-list request queues behind it with no acquisition timeout:
  systemwide infinite loading with all healthchecks green.
- **Fix:**
  ```python
  STREAM_IDLE_TIMEOUT_S = float(os.environ.get("STREAM_IDLE_TIMEOUT_S", "120"))
  LOCK_WAIT_S = float(os.environ.get("LOCK_WAIT_S", "15"))

  try:
      await asyncio.wait_for(_inference_lock.acquire(), timeout=LOCK_WAIT_S)
  except asyncio.TimeoutError:
      yield b'data: {"error": "GPU busy: an inference is already running."}\n\n'
      yield b"data: [DONE]\n\n"
      return
  try:
      async with request.app.state.http.post(
          ..., timeout=aiohttp.ClientTimeout(total=None, sock_connect=10,
                                             sock_read=STREAM_IDLE_TIMEOUT_S),
      ) as upstream:
          async for chunk in upstream.content.iter_any():
              yield chunk
  finally:
      _inference_lock.release()
  ```
  `sock_read` bounds *inter-chunk* silence, not total duration — long generations stay
  legal; a hung socket frees the GPU mutex in ≤120 s with a user-visible error. Also emit
  `data: [DONE]\n\n` after the mid-stream error frame (line 147) so OpenAI-compatible
  clients terminate instead of spinning.

### 🟠 P2-3 Pipeline layer repeats the same pattern one level up
- **File:** `pipelines/langgraph_router.py:164, 182, 197-204`
- **Root cause:** `self._request_lock` serializes **all users and all chats** in the
  pipelines process and is held across the entire SSE relay, again with
  `ClientTimeout(total=None, sock_connect=10)` and no read timeout. A hung orchestrator
  stream deadlocks every conversation. The lock is redundant — GPU serialization already
  lives in the gateway. Secondary: `iter_sse_events` (line 125) grows `buffer` without
  bound if the upstream never emits `\n\n`.
- **Fix:** Delete `_request_lock` (or scope it per-thread_id). Add
  `sock_read=STREAM_IDLE_TIMEOUT_S`. Cap the SSE buffer (e.g. 1 MiB → yield an error
  frame and abort). Reuse one `aiohttp.ClientSession` created in `on_startup` instead of
  per request.

### 🟠 P2-4 No token-level streaming exists anywhere in the chain
- **Files:** `backend/langgraph_orchestrator.py:95-127, 612-669`;
  `pipelines/langgraph_router.py:209-222`
- **Root cause:** Every `_llm_call` is a **non-streaming** completion (up to 120 s); the
  orchestrator's `/stream` only emits node-boundary events, and the pipeline forwards
  whole messages. The user watches a blank assistant bubble for the full duration of each
  graph node. The Phase-2 worry about "dropped tokens under UI lag" is moot today —
  tokens aren't streamed at all; the symptom users see is indistinguishable from a hang.
- **Fix:** In `Simulator`/`Factual_Shortcircuit_Node` call the gateway with
  `"stream": true`, parse deltas, and re-emit `event: graph_token` SSE frames; in
  `langgraph_router._stream` yield those delta strings directly. For buffering: an
  `asyncio.Queue(maxsize=4096)` between upstream reader and SSE writer in the gateway
  decouples LM Studio's production rate from a lagging client; on overflow, coalesce
  adjacent deltas (concatenate strings) rather than drop. TCP backpressure already
  prevents loss — the queue prevents the *reader* from stalling LM Studio's socket.

### 🟠 P2-5 Localized dependency timeouts: 30 s / 45 s / 180 s / 300 s vs the 5 s policy, plus an egress violation
- **Files:** `deep-web-mcp/server.py:706` (SearXNG 30 s),
  `docker-compose.yml:378` (crawl4ai page timeout 45 s),
  `workspace/catalog-tools/web_research.py:56, 111-137, 261-329` (180 s/hop × 4 hops),
  `workspace/catalog-tools/docling_ingestion.py:118` (300 s)
- **Root cause:** Worst-case interactive path is ~12 minutes of spinner. Worse,
  `web_research._validate_link` issues **direct external GETs from the open-webui
  container**, bypassing deep-web-mcp's `ALLOWED_TARGET_HOSTS` egress control — and since
  `llm-net` has IP masquerade disabled, every probe is a guaranteed 8 s timeout. Hops×links
  of pure dead time, and an architectural hole in the air-gap story.
- **Fix:** Tiered budgets with graceful structured errors:
  - SearXNG query: **5 s** (`server.py:706`) — local service; if it can't answer in 5 s it
    won't answer in 30.
  - Per-hop research budget: 30 s; tool total: 90 s; return partial results with
    `"ceiling_hit": true` rather than erroring.
  - Delete client-side link validation from `web_research.py` (deep-web-mcp `research.py`
    already validates inside the controlled perimeter) — removes both the latency and the
    egress bypass.
  - Docling: keep 300 s but submit as async task + status polling (docling-serve supports
    it) so no chat turn ever awaits it inline.
  Every timeout returns `{"status":"error","error_code":...,"reason":...}` immediately —
  the JSON the tools already emit — never an unbounded await.

### 🟠 P2-6 telemetry-gateway strips its own database password
- **Files:** `services/telemetry-gateway/app.py:105-113`; `docker-compose.yml:452`;
  `infra/postgres/init/002-create-telemetry-role.sh:13`
- **Root cause:** `_ro_dsn()` rewrites `postgresql://USER:PASS@…` →
  `postgresql://telemetry_ro@…`, deleting the credential. The compose DSN *already*
  connects as `telemetry_ro`; after the rewrite asyncpg has no password and scram auth
  fails → the pool can't start → crash-loop (masked by `restart: unless-stopped`).
  Secondary: the password `telemetry_readonly_changeme` is hardcoded in both compose and
  the init script.
- **Fix:** Delete `_ro_dsn` and use `POSTGRES_OPS_URL` verbatim; assert read-onlyness at
  startup with `SELECT current_user` + a `SET TRANSACTION READ ONLY` probe instead.
  Move the password to `${TELEMETRY_RO_PASSWORD:?…}` in `.env` and parameterize the init
  script.

### 🟡 P2-7 Conversation state registry: leak + cross-chat contamination
- **File:** `pipelines/langgraph_router.py:163, 211-213, 248`
- **Root cause:** `_thread_registry: dict[user_id → thread_id]` grows forever (leak), is
  lost on restart (state loss), and is keyed by **user**, so two concurrent chats from
  the same user share one LangGraph thread — replies cross-contaminate.
- **Fix:** Key by chat id (`body["metadata"]["chat_id"]` is present in Open WebUI pipe
  calls); store in `redis-hitl` with `SETEX` (TTL 24 h) so it survives restarts and
  self-evicts. Same pattern fixes recovery after a pipelines container bounce.

### 🟡 P2-8 `/interrupt` cannot cancel streamed runs
- **File:** `backend/langgraph_orchestrator.py:593-594, 612-669, 684-695`
- **Root cause:** `/invoke` registers its task in `_active_tasks`; `/stream` never does.
  The pipeline drives normal traffic through `/stream`, so a retroactive edit cancels
  nothing — the old graph keeps consuming GPU while the replacement starts.
- **Fix:** Register a cancellation handle per thread_id inside `event_generator` (wrap
  the `astream_events` loop in a task stored in `_active_tasks`, `finally`-popped), so
  `/interrupt` works uniformly.

### 🟡 P2-9 Per-call model rediscovery through the GPU mutex
- **File:** `backend/langgraph_orchestrator.py:80-92, 104`
- **Root cause:** Every `_llm_call` first GETs `/v1/models`, which (P1-8) queues behind
  the inference lock — each graph node pays an extra serialized round-trip.
- **Fix:** Cache the resolved model id for 30 s (module-level `(model, expires_at)`),
  invalidate on gateway 4xx/5xx. Combined with the gateway-side lock-free `/v1/models`,
  the round-trip disappears from the critical path.

### 🟡 P2-10 Zero-vector "semantic" retrieval
- **File:** `backend/langgraph_orchestrator.py:236-244, 361-374`
- **Root cause:** Both `query_points` and `upsert` use `[0.0] * 768`. Cosine similarity
  against a zero vector is undefined; Qdrant returns arbitrary points. The Lorekeeper's
  "memory" is a random-row fetch and always has been.
- **Fix:** Embed via the gateway (`POST /v1/embeddings`, already proxied) at both write
  and query time; fall back to `DEFAULT_CANON` (current behavior) when embeddings are
  unavailable.

### ⚪ P2-11 HITL broker's `is_connected` never connects
- **File:** `backend/hitl_broker.py:110-134`; consumed at
  `backend/langgraph_orchestrator.py:464-466`
- **Root cause:** `aioredis.from_url()` is lazy; `connect()` cannot fail, so the
  orchestrator's startup gate (`if not broker.is_connected: raise`) is decorative. With
  Redis down, every high-risk authorization silently auto-denies (safe direction, but
  invisible).
- **Fix:** `await self._client.ping()` in `connect()`; surface broker state in `/health`
  with a live `PING`.

### ⚪ P2-12 deep-web-mcp task registry never evicts; SSE poller never cancels
- **File:** `deep-web-mcp/server.py:157, 648, 950, 968-997`
- **Root cause:** `_task_registry` grows monotonically (unbounded memory in a
  long-running container); on client disconnect the generator dies but `bg_task` keeps
  crawling — orphaned Chromium work.
- **Fix:** TTL sweep (delete entries with `status in {done,error}` older than 10 min) on
  a background task; wrap the generator body in `try/finally: bg_task.cancel()` guarded
  by a "client gone" check (`request.is_disconnected()`).

### ⚪ P2-13 Keyword tripwire fails any prompt containing "contradiction"
- **File:** `backend/langgraph_orchestrator.py:342-343`
- **Root cause:** Demo artifact: any user input containing the literal word forces three
  full LLM loops then fail-safe termination — minutes of GPU burn for an innocent prompt.
- **Fix:** Delete the clause; the LLM judgment above it is the actual check.

---

## Phase 3 — UI Responsiveness & State

### 🟠 P3-1 WebSocket support disabled forces socket.io long-polling
- **File:** `docker-compose.yml:254` (`ENABLE_WEBSOCKET_SUPPORT: "false"`)
- **Root cause:** Open WebUI's event channel (tool `__event_emitter__` status frames,
  task updates, typing indicators) runs over socket.io. With websockets off it degrades
  to HTTP long-polling: per-event request overhead, head-of-line delays under load — the
  observed "UI lags during heavy hardware activity." Everything is bound to
  `127.0.0.1`; websockets add zero exposure here.
- **Fix:** `ENABLE_WEBSOCKET_SUPPORT: "true"`. Chat token streams are fetch/SSE and
  unaffected either way, but tool/status events become push.

### 🟡 P3-2 Input drafts survive reload but not tab loss; mid-stream tokens unrecoverable
- **Files (vendored, verified):**
  `open-webui-0.9.6/src/lib/components/chat/Chat.svelte:218, 2873, 2879` —
  drafts persist to `sessionStorage` (`chat-input-{chatId}`)
- **Current behavior:** Reload-safe per tab; lost on tab close/crash. Completed messages
  persist server-side; tokens of an in-flight generation are lost client-side on socket
  drop, and with the current non-streaming graph (P2-4) the entire response is lost.
- **Fix within the existing override architecture** (no vendor fork): add a
  `patch_frontend.mjs` rule rewriting the two `sessionStorage` draft calls to
  `localStorage` with a 7-day TTL wrapper; server-side, the orchestrator already persists
  `graph_complete` events to `ops.orchestration_events` — exposing
  `GET /debug/state/{thread_id}` data through the pipeline on reconnect provides
  generation recovery without new frontend code. Fixing P2-4 (true streaming) plus
  Open WebUI's existing DB persistence covers the remaining gap.

### 🟡 P3-3 Brutalist style patch forces full-subtree recalc on every stream tick
- **File:** `workspace/open-webui-overrides/patch_frontend.mjs:142-165`
- **Root cause:** `.brutalist-artifact :global(*) { … !important }` matches every
  descendant of a flagged block; during token streaming the markdown tree re-renders
  repeatedly and the universal selector re-evaluates each time — measurable main-thread
  cost on long code blocks.
- **Fix:** Scope to the elements actually styled
  (`:global(pre), :global(code), :global(td), :global(th), :global(blockquote)`) and
  drop `!important` where the cascade already wins. The marker-regex token pass added to
  `MarkdownTokens.svelte` is O(tokens) and fine.

### 🟡 P3-4 Open WebUI container runs with SYS_ADMIN + unconfined seccomp + rw cgroups
- **File:** `docker-compose.yml:280-287`
- **Root cause:** `cap_add: SYS_ADMIN`, `seccomp=unconfined`, and
  `/sys/fs/cgroup:/sys/fs/cgroup:rw` on the most exposed container in the stack directly
  contradict the hardened posture (every other service drops capabilities). This is the
  classic container-escape configuration. If it was added for in-app code execution,
  isolate that in a dedicated sandbox service instead.
- **Fix:** Remove all three; restore `cap_drop: [ALL]` parity with the other services.
  Decision needed if a feature genuinely requires it — none of Open WebUI 0.9.6's default
  features do.

### ⚪ P3-5 Main-thread markdown under long histories (upstream, mitigate via batching)
- **Context:** Open WebUI parses markdown on the main thread (vendor design). The cheap,
  no-fork mitigation is server-side delta coalescing: in the gateway/pipeline stream
  relay, batch deltas into ~30 ms flushes (`asyncio.Queue` + drain loop from P2-4's
  buffer) so the UI re-renders ~33×/s instead of per token. Combined with P3-1 this is
  the bulk of perceived smoothness on the 9950X.

---

## Cross-cutting configuration notes

| Item | File | Note |
| --- | --- | --- |
| Stale comment: "LM Studio must listen on 127.0.0.1:1234" | `.env.example:12` | Stack uses 4321 everywhere; fix the doc to prevent operator misconfig. |
| `WEB_SEARCH_CONCURRENT_REQUESTS: "1"`, `WEB_LOADER_CONCURRENT_REQUESTS: "1"` | `docker-compose.yml:247-249` | Local SearXNG can take 4-6 concurrent; current setting serializes multi-query RAG searches. |
| Legacy duplicates (`vram_arbiter.py`, `swarm_orchestrator.py` with hardcoded `qdrant…/collections/test`, non-existent unload endpoint) | repo root | Mark deprecated or delete; they confuse which arbiter/orchestrator is canonical. |
| `_GAP_PATTERNS` hardcode "2024 2025" | `web_research.py:43-47` | Stale year tokens bias sub-queries; derive from system date. |
| `deep-web-mcp/database.py:59-77` | sync sessions without `try/finally` | `db.close()` skipped on exception → connection leak under error load; also docstring claims AES-256 but Fernet is AES-128-CBC. |

## Recommended fix order

1. **P2-1** (pipelines stub) — nothing else matters until the model path is alive.
2. **P2-2 / P2-3 / P1-8** — the lock+no-read-timeout pattern in both stream relays; this
   is the systemwide-freeze class.
3. **P1-1 / P1-2 / P1-3** — make the VRAM safeguard real (host-side, `lms unload`,
   NVML-measured ceiling with UI reserve).
4. **P2-6** (telemetry DSN), **P1-5** (knowledge API contract) — dead features that report
   green.
5. **P2-4 + P3-1 + P3-5** — perceived-latency package: true token streaming, websockets,
   delta batching.
6. **P2-5** — timeout policy + egress bypass removal.
7. Remaining 🟡/⚪ hygiene items.

All proposed fixes use components already vendored in the stack (aiohttp/httpx, Redis,
asyncpg, NVML on the host, LM Studio CLI/REST) — no new external dependencies, fully
offline-compatible.
