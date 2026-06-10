# Open WebUI Configuration Validation

**Final status: PASS**

**Mission target:** the already-running, full-feature Open WebUI v0.9.6 deployment  
**Validation execution:** 2026-06-09, America/Toronto  
**Requested report date:** 2026-06-10

## Executive Summary

The live Open WebUI stack was stabilized without recreating containers from the
incompatible checked-in hardened compose topology. Useful live configuration,
credentials, stable model/tool/function IDs, and user data were preserved.

All required completion tests, the resource-limited Python tool test, web
search, document retrieval, backend health checks, browser-console review, and
the final clean service-log review passed. Qwen3.5 completed multiple clean
responses well inside the 180-second acceptance threshold and remains the
default model.

The final clean log window, `2026-06-09T21:16:09` through
`2026-06-09T21:16:35`, contained no `ERROR`, `Traceback`, `Exception`, `FATAL`,
or `panic` entries in the reviewed services.

## Scope And Safety

- The running legacy/full-feature deployment was the mission target.
- No containers were recreated from the checked-in hardened compose topology.
- No public API, model, tool, function, or knowledge IDs were changed.
- No database schema was changed.
- Existing credentials were neither displayed nor replaced.
- No state-changing Calendar or Home Assistant action was performed.
- Broad or state-changing tools remain installed but are manually opt-in.

## Backup And Baseline Evidence

A consistent, timestamped backup was created before configuration changes:

`C:\open-webui-master\backups\webui_validation_20260609_201513`

Contents:

- `webui.db`: consistent SQLite backup
- `config.json`: live WebUI configuration backup
- `searxng_settings.yml`: pre-change SearXNG configuration backup

The backed-up SearXNG settings SHA-256 is:

`EDE86C3B8A8D8C9299190E3F72F1768B1C787A0C349B3DFB4C5044E925DBEB08`

The initial baseline showed the following active issues:

| Area | Initial observation |
|---|---|
| LangGraph orchestrator | HTTP 503; Redis connection was lost |
| Docling | Configured URL `http://docling:5001` did not resolve |
| SearXNG | Default engines produced plugin-load, CAPTCHA, and rate-limit errors |
| Sandbox tool | `Sandbox did not start in time`; gVisor `systrap` panicked with `failed to create a syscall thread` |
| Structured-plan completion | Model invoked built-in `write_note` instead of directly answering |
| Web-search completion | Model repeatedly selected an attached Bash tool and reached the eight-tool-call limit |
| ComfyUI MCP pipeline | Loader error: `No Function class found` |

## Repairs And Configuration

### Backend Repairs

- Restarted only `langgraph-orchestrator`; its Qdrant and Redis dependencies
  returned healthy and remained healthy.
- Corrected the Docling URL to `http://docling-serve:5001`.
- Narrowed SearXNG to a reliable engine pool:
  - removed default `ahmia` and `torch` engines
  - disabled `brave`, `duckduckgo`, and `google`
  - disabled the limiter for this internal-only service
- Fixed the disabled ComfyUI MCP pipeline loader contract by changing its
  implementation class from `Pipeline` to the v0.9.6-compatible `Pipe`.
- Repaired the resource-limited Python/Bash Sandbox:
  - selected gVisor `--platform=ptrace`
  - ran the OCI workload as UID/GID 0 inside gVisor
  - retained an empty OCI capability set and `noNewPrivileges=true`
  - retained mandatory resource limiting

### Reliable Defaults

| Setting | Final value |
|---|---|
| Default model | Qwen3.5 (`qwen35`) |
| Fallback preset | Preset - Vision and Tools - Qwen2.5 VL 7B |
| LM Studio | `http://host.docker.internal:4321/v1` |
| Ollama | Disabled |
| Direct connections | Disabled |
| Dynamic base-model cache | Disabled |
| Default user role | `pending` |
| New signups | Disabled |
| Community sharing | Disabled |
| API keys | Disabled |
| Arena all-model mode | Disabled |
| Task model | Current Model |
| Title generation | Enabled |
| Follow-up generation | Disabled |
| Tag generation | Disabled |
| Web search | Enabled through SearXNG |
| Web loader | External Crawl4AI proxy |
| Audio | Local Kokoro |
| Code execution | Pyodide plus resource-limited sandbox tool |
| Image generation | Disabled |
| Experimental user memory | Disabled |
| Calendar/Home Assistant user connections | Disabled |

Chat-incompatible `kokoro_gguf` and
`text-embedding-nomic-embed-text-v1.5` are hidden.

### Default Model Tools

Qwen3.5 automatically attaches only these safe defaults:

- Inline Visualizer (`inline_visualizer`)
- YouTube Transcript Provider (`youtube_transcript_provider`)
- Resource-limited Python/Bash Sandbox (`run_code_py`)

Final Qwen3.5 capabilities:

- web search: enabled
- code interpreter: enabled
- image generation: disabled
- terminal: disabled
- built-in tools: disabled

The built-in tools capability was disabled after the structured-plan test
incorrectly created a note. The generated validation note was deleted.

## Rename History

Only display metadata changed; stable IDs and references were preserved.

| Stable ID / original | Final display name |
|---|---|
| `swarm_controls` / Swarm Controls | Tool - Agent Swarm - Manual Controls |
| `mcp_app_bridge` / MCP App Bridge | Tool - MCP Apps - UI Bridge |
| `deep_web_ecosystem_tools` / Deep Web Ecosystem Tools | Tool - Deep Web - Search and Fetch |
| `calendar_ecosystem_tools` / Calendar Ecosystem Tools | Tool - Calendar - Local Management |
| `home_assit` / Home assistant | Tool - Home Assistant - Local Control |
| `langfuse_filter` / Langfuse Filter. | Function - Observability - Langfuse Filter |
| `comfy_mcp_pipeline` / comfy-mcp-pipeline | Pipeline - Image Generation - ComfyUI MCP |
| `qwen257b` / qwen2.57b | Preset - Vision and Tools - Qwen2.5 VL 7B |
| `moyclark` / MoyClark | Preset - Roleplay - Galadriel |
| roleplay knowledge / roleplay instructions | Knowledge - Roleplay - Galadriel |

## Backend Health Validation

| Component | Final validation | Result |
|---|---|---|
| Open WebUI | `/health` returned `{"status":true}` | PASS |
| LM Studio | `/v1/models` returned HTTP 200 and 13 models | PASS |
| Qdrant | Internal `/healthz`: `healthz check passed` | PASS |
| Redis | `redis-cli ping`: `PONG` | PASS |
| SearXNG | JSON search returned results; first result was `https://openwebui.com/` | PASS |
| Docling | `/health`: `{"status":"ok"}` | PASS |
| Crawl4AI | Internal `/health`: status `ok`, version `0.5.1-d1` | PASS |
| Crawl4AI proxy | Real `POST /crawl` against a reachable local URL returned HTTP 200 | PASS |
| Kokoro | `/v1/models` returned HTTP 200 | PASS |
| Deep Web MCP registry | SSE handshake returned HTTP 200 | PASS |
| LangGraph orchestrator | Health returned Qdrant and Redis `true`; six sustained checks plus three final checks passed | PASS |
| Calendar MCP | `/health`: healthy and ready | PASS |
| Home Assistant MCP | `/health`: `{"status":"ok"}` | PASS |

All relevant containers were running. Docker health checks reported healthy for
Open WebUI, Qdrant, Redis, Crawl4AI, and the orchestrator. The orchestrator
restart count did not increase after its intentional repair restart.

The Crawl4AI proxy correctly rejected malformed test payloads and accepted its
real URL-array schema. An external `https://example.com` crawl encountered a
site-navigation failure; the final reachable local crawl passed with no active
service error. External-site crawl success remains dependent on target-site and
network behavior.

## Functional Validation

Each required completion ran in a clean chat.

| Test | Timing | Result | Evidence |
|---|---:|---|---|
| Basic one-sentence response | 44.2 s | PASS | Normal one-sentence response |
| Structured five-step plan, retest | 45.1 s | PASS | Direct, correctly structured five-step response |
| Python sum of squares, 1 through 100 | 77 s | PASS | Real `run_python_code` invocation returned `338350` |
| Full technical-readiness report | 117 s | PASS | Complete report with summary, architecture, health, risks, and GO recommendation |
| Isolated built-in web search | 55 s | PASS | Search-backed answer cited official `https://openwebui.com/` |
| Ephemeral document retrieval | 178 s | PASS | Retrieved exact unique marker and phrase with a citation |

Qwen3.5 passed more than twice within 180 seconds and therefore remains the
default model. The `qwen257b` preset remains the configured fallback.

### Tool Self-Test

The resource-limited Python/Bash Sandbox built-in self-test passed:

- simple Python execution
- simple Bash execution
- invalid Python syntax handling
- invalid Bash syntax handling
- timeout enforcement
- RAM-limit enforcement

The required real UI tool invocation calculated:

`Sum of squares from 1 to 100: 338350`

### Web Search

The first clean-chat attempt selected the attached Bash tool repeatedly instead
of the built-in web-search path and reached the eight-tool-call limit. The
root cause was tool-selection competition under native function calling.

For the retest, Qwen3.5 was temporarily isolated to the built-in web-search
feature. The retest passed in 55 seconds with a cited official result. The exact
safe tool attachments and both intended default features were restored
immediately afterward. A final direct SearXNG search returned HTTP 200 with ten
results in approximately 365-496 ms.

### Document Retrieval

An ephemeral collection named `Validation Probe 2026-06-10` was created with a
unique marker and phrase. Qdrant indexing succeeded, and a clean chat retrieved
the exact marker and phrase with one source citation. The generated collection
and local temporary document were deleted after validation. No existing
knowledge artifact was removed.

## Failure, Fix, And Retest Record

| Failure | Root cause | Fix | Retest |
|---|---|---|---|
| Orchestrator HTTP 503, Redis disconnected | Lost live Redis connection | Restarted orchestrator only | Sustained health checks all HTTP 200 |
| Docling hostname did not resolve | Stale service name `docling` | Changed URL to `docling-serve` | Health returned HTTP 200 |
| Structured plan created a note | Built-in tools were too broad for defaults | Disabled built-in tools; deleted generated note | Direct five-step answer passed |
| Sandbox failed to start | gVisor `systrap` syscall-thread panic; nested user-namespace incompatibility | Selected `ptrace` and constrained root inside gVisor while preserving limits | Full self-test and UI calculation passed |
| Initial web-search chat exhausted tool-call limit | Attached-tool selection competed with built-in search | Isolated web search for validation; restored exact defaults afterward | Search-backed clean-chat answer passed |
| SearXNG engine errors | Unreliable/CAPTCHA/rate-limited default engines | Removed or disabled failing engines | Final search and clean logs passed |
| Comfy pipeline loader error | v0.9.6 expected class `Pipe` | Updated disabled pipeline class | Direct loader test passed |

## Browser And Log Review

- The final browser-console review contained no warnings or errors.
- A fresh browser context also contained no warnings or errors.
- The final clean service-log window reviewed:
  - `open-webui`
  - `qdrant`
  - `redis-cache`
  - `searxng`
  - `docling-serve`
  - `crawl4ai`
  - `crawl4ai-proxy`
  - `kokoro-tts`
  - `langgraph-orchestrator`
  - `deep-web-mcp`
  - `calendar-mcp`
  - `ha-mcp`
- No current `ERROR`, `Traceback`, `Exception`, `FATAL`, or `panic` entries
  were found in the final clean window.

## Files And Live State Changed

- Live Open WebUI SQLite/config state: narrowly scoped settings, metadata,
  model defaults, and tool/function code changes described above
- `C:\open-webui-master\data\searxng\settings.yml`: reliable internal search
  engine configuration
- `C:\open-webui-master\data\searxng\limiter.toml`: documents the intentional
  internal-only limiter disablement
- This validation report

## Residual Risks

1. **Compose reproducibility drift:** the checked-in hardened compose topology
   does not represent the running full-feature mission target. Deploying it
   without reconciliation could remove or alter live capabilities. It was not
   deployed during this mission.
2. **External search/crawl variability:** external engines and sites may add
   CAPTCHA, rate limits, egress restrictions, or navigation failures. The
   reliable engine pool and live bridge are healthy, but target behavior is
   outside this stack's control.
3. **Native tool selection:** a model can choose an attached safe tool instead
   of a built-in feature. The final default attachments are constrained, and
   isolated feature validation confirmed that built-in web search works.

## Final Decision

**PASS - GO for the validated local Open WebUI deployment.**

All four required completion tests, the real resource-limited tool invocation,
web search, document retrieval, backend health checks, browser-console review,
and final service-log review succeeded. No unresolved active UI, backend, or
tool errors remain.
