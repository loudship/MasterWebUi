# Deep Web MCP Debugging and Validation

**Date:** 2026-06-10  
**Decision:** **PASS - GO**

## Scope

Debug and improve the live Docker-based Deep Web MCP integration, then prove
search, extraction, MCP discovery, security controls, dashboard behavior, and
main Open WebUI prompt completion with real requests.

## Defects Fixed

- REST routes were hidden because the MCP SSE application was mounted before
  FastAPI routes were registered. The MCP transport is now mounted last.
- The checked-in MCP tool signatures had drifted from the live Open WebUI tool
  bridge. The working `fetch_deep_web_data` and `search_deep_web_database`
  contracts are preserved.
- The live credential-vault SQLite path caused a restart loop when its parent
  directory did not exist. SQLite parent directories are now created
  automatically, and the live container uses the persistent
  `deep-web-mcp-data` volume.
- Extraction used Crawl4AI's removed `markdown_v2` property. It now consumes
  the current `markdown` / `MarkdownGenerationResult` contract.
- Search could return stale or oversized results. It now returns live,
  normalized, relevance-ranked results, a compact five-result set, and a
  `best_match` field for reliable prompt completion.
- Dashboard monitoring used a fake extraction-status task ID, producing noisy
  404 logs. It now probes the real `/health` endpoint.
- Misleading direct-Tor behavior was removed. Direct public extraction is
  guarded; private, reserved, and onion targets are rejected.

## Live Proof

### Docker and Health

- `deep-web-mcp`, `monitor-daemon`, and `open-webui` remained running.
- Web Tools Control Center reported **7/7 connected** and **100% health**.
- Deep Web MCP `/health` returned both MCP tools:
  `fetch_deep_web_data` and `search_deep_web_database`.
- Final Deep Web MCP and monitor-daemon error-log scans were clean.

### Real Search

Query: `Crawl4AI official GitHub repository`

- Route: `searxng_internal`
- Source: `live`
- Result count: `5`
- Ranked `best_match`:
  `https://github.com/unclecode/crawl4ai`

### Real Extraction and Security

- Extracted `https://example.com` through Deep Web MCP.
- Returned HTTP 200 content beginning with `# Example Domain`.
- Route was reported as `direct`.
- Attempted extraction of `http://127.0.0.1:8080`.
- Request was rejected because the target resolved to blocked address
  `127.0.0.1`.

### MCP Transport

- MCP SSE discovery returned:
  `fetch_deep_web_data`, `search_deep_web_database`.
- MCP search invocation returned the live ranked GitHub result.
- MCP extraction invocation returned sanitized Example Domain content.

### User Interfaces

- Dedicated Deep Web MCP dashboard workspace rendered real search results.
- Dashboard extraction rendered Example Domain content.
- Dashboard displayed the private-address rejection.
- Browser console contained no warning or error entries during dashboard proof.
- Main Open WebUI prompt used source
  `deep_web_ecosystem_tools/search_deep_web_database` and returned the exact
  full URL `https://github.com/unclecode/crawl4ai`.

## Verification Commands

- `python -m pytest tests/test_deep_web_mcp_contract.py tests/test_monitor_dashboard.py tests/test_hardened_policy.py -q`
  - **24 passed**
- `python -m py_compile deep-web-mcp/server.py deep-web-mcp/database.py monitor_daemon.py`
  - **passed**
- `docker compose --profile offline-tools config --quiet`
  - **passed**
- `git diff --check`
  - **passed**; only Windows LF-to-CRLF notices were emitted.

## Residual Notes

- "Deep Web MCP" is a search and guarded direct-extraction integration. It does
  not claim onion/Tor routing.
- Open WebUI tool toggles are chat/model state. The dedicated
  `Preset - Vision and Tools - Qwen2.5 VL 7B` preset includes the Deep Web tool
  and produced the cleanest prompt-completion proof.

## Final Decision

**PASS - GO.** Deep Web MCP search, extraction, MCP transport, dashboard UI,
security rejection, and main Open WebUI prompt completion were all verified
with live requests.
