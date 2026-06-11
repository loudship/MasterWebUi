# Web Tools Control Center Validation

Date: 2026-06-10  
Target: Live local Docker deployment and Open WebUI at `http://localhost:8080/`  
Control Center: `http://127.0.0.1:19000/`

## Final Decision

**PASS - GO**

The local Web Tools Control Center is deployed and verified. Its main overview,
dedicated Web Search, Crawl4AI, Firecrawl-compatible, and Monitor interfaces all
work. Real web-search and tool-assisted prompt completions also passed in the
main Open WebUI after full UI synchronization.

## Implemented Architecture

- Main overview with live connectivity, latency, and health for seven services
- Dedicated SearXNG search workspace with configurable engine pool, category,
  language, safe-search level, and result count
- Dedicated Crawl4AI extraction workspace with configurable timeout
- Dedicated Firecrawl workspace
  - Uses native Firecrawl when `FIRECRAWL_URL` is configured
  - Uses a clearly labeled local Crawl4AI compatibility bridge otherwise
- Dedicated semantic-drift monitor workspace
- Per-tool UI settings persisted in browser local storage
- Direct link from the control center to the main Open WebUI

## Live Service Result

The final control-center overview reported:

- Connected: `7 / 7`
- Stack health: `100%`
- Open WebUI: reachable
- SearXNG Search: reachable
- Crawl4AI Proxy: reachable
- Deep Web MCP: reachable
- Qdrant: reachable
- LangGraph Orchestrator: reachable
- Browserless: reachable
- Firecrawl mode: `crawl4ai_compatibility`

Native Firecrawl was not configured in the live deployment. The dedicated
Firecrawl interface was verified through the local compatibility bridge and
returned real extracted Markdown.

## Real Interface Tests

| Interface | Real test | Result |
|---|---|---|
| Overview | Loaded live service cards and health | PASS - 7 cards, 100% |
| Web Search | Searched `Open WebUI documentation` | PASS - 7 visible results |
| Crawl4AI | Extracted `https://example.com` | PASS - real Example Domain Markdown |
| Firecrawl | Scraped `https://example.com` in compatibility mode | PASS - real Example Domain Markdown |
| Monitor | Loaded current monitor summary and history | PASS |

Both the control-center browser console and the main Open WebUI browser console
were clean in the final review.

## Main Open WebUI Prompt Proof

### Built-In Web Search

Prompt:

`Use the built-in web search to find the official Open WebUI homepage. Reply
with only the homepage URL and one cited source.`

Result:

- Completed in the main Open WebUI
- Returned `https://openwebui.com/`
- Included a web-search source citation
- PASS

The first search attempt reproduced attached-tool competition. Qwen3.5 was
temporarily isolated to built-in features for the retest, then its exact safe
attachments were restored.

### Tool-Assisted Prompt Completion

Prompt:

`Use the Python & Bash Sandbox tool, specifically run_python_code, to calculate
the sum of cubes from 1 through 20. Return the tool result and final answer.`

Result:

- Real `run_python_code` tool invocation
- Tool result: `44100`
- Final answer: `44,100`
- PASS

Final Qwen3.5 state was verified:

- `toolIds`: `inline_visualizer`, `youtube_transcript_provider`, `run_code_py`
- `defaultFeatureIds`: `web_search`, `code_interpreter`

## Backend Fixes Applied

- Replaced the monitor-only page with the multi-interface Web Tools Control Center
- Added normalized control APIs for overview, search, crawl, Firecrawl, and config
- Added web URL validation and bounded per-request timeouts
- Added reliable service probes for Open WebUI, SearXNG, Crawl4AI Proxy,
  Deep Web MCP, Qdrant, LangGraph, and Browserless
- Removed live Tor proxy routing from SearXNG and Crawl4AI after proving it caused
  CAPTCHA suspension and `ERR_TUNNEL_CONNECTION_FAILED`
- Selected Bing as the reliable SearXNG default and disabled noisy blocked engines
- Preserved the separate checked-in hardened air-gap policy contracts

## Automated And Docker Validation

- `python -m pytest tests/test_monitor_dashboard.py tests/test_hardened_policy.py -q`
  - `19 passed`
- `python -m py_compile monitor_daemon.py`
  - PASS
- `docker compose --profile offline-tools config --quiet`
  - PASS with validation-only required environment values
- `docker build -f Dockerfile.monitor -t monitor-daemon:hardened .`
  - PASS
- `git diff --check`
  - PASS
- Final clean service-log review found no active application errors in:
  - monitor-daemon
  - open-webui
  - searxng
  - crawl4ai
  - crawl4ai-proxy
  - deep-web-mcp
  - browserless

## Files Changed

- `monitor_daemon.py`
- `monitor_dashboard.html`
- `Dockerfile.monitor`
- `docker-compose.yml`
- `data/searxng/settings.yml`
- `tests/test_monitor_dashboard.py`
- This report

## Backup

Before temporarily isolating Qwen3.5 for the built-in web-search retest, the live
database and original model metadata were backed up under:

`C:\open-webui-master\backups\web_tools_ui_20260610`

## Residual Risk

- Native Firecrawl requires an operator-provided `FIRECRAWL_URL` and optional
  `FIRECRAWL_API_KEY`. Until configured, the dedicated interface intentionally
  reports and uses the verified Crawl4AI compatibility bridge.
- External search engines and target sites can still change rate limits or
  anti-bot behavior. The selected live search path and crawler were working in
  the final validation window.
