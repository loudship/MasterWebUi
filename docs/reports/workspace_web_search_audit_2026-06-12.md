# Workspace and Web Search Audit - 2026-06-12

## Final Verdict

**DEGRADED PASS.** The five Workspace menus, live persistence, deterministic research router, `web search` model, documentation, and primary research flows are deployed and verified. Four pre-existing integrations remain dependency-limited but now fail safely and are explicitly documented below.

## Live Deployment

- Open WebUI: `open-webui:workspace-research`, host `127.0.0.1:3000` to container `8080`, healthy.
- Deep Web MCP: `deep-web-mcp:workspace-research`, internal port `8000`, healthy.
- Rollback backup: `backups/workspace_web_search_20260612_110530`.
- Retained rollback containers include `open-webui-pre-workspace-20260612_112404` and `deep-web-mcp-pre-validator-20260612_111948`.
- Live database and authenticated APIs were treated as authoritative; the stale checkout database was not modified.

## Workspace Pillars

| Pillar | Browser | API persistence probe | Live count | Result |
| --- | --- | --- | ---: | --- |
| Models | HTTP 200, no console errors | create/update/read/delete, no residue | 5 | PASS |
| Prompts | HTTP 200, no console errors | create/update/read/delete, no residue | 6 | PASS |
| Knowledge | HTTP 200, no console errors | create/update/read/delete, no residue | 3 | PASS |
| Tools | HTTP 200, no console errors | create/update/read/delete, no residue | 10 | PASS |
| Functions | HTTP 200, no console errors | create/update/read/delete, no residue | 0 | PASS |

The new admin-only `/workspace/functions` route is live. Catalog status now reports `function: 0` explicitly instead of omitting an empty pillar.

## Web Research Validation

- General router: PASS; one-hop SearXNG lookup, concurrent validation, structured trace, verified clickable Markdown links.
- Deep router: PASS; bounded iterative lookup, coverage-gap query, extraction status, citations, and Markdown report.
- Authenticated persistence: PASS; report persisted into `Knowledge - Research - Web Search Reports`.
- General model routing: PASS; `web-search` emits `research_web(..., strategy="general")`.
- Explicit Deep override: PASS; emits `strategy="deep", max_iterations=3, max_sources=8`.
- Inferred Deep intent: PASS; “Investigate comprehensively…” emits bounded Deep Research.
- Direct API calls without `tool_ids`: expected Open WebUI API behavior; the frontend supplies model-attached tool IDs. UI-equivalent calls invoked the dedicated tool.

## Tool Matrix

| Tool | Trigger and role | Synergy | Live evidence | Result |
| --- | --- | --- | --- | --- |
| `web_research` | Current facts or investigative research | SearXNG, Crawl4AI, Knowledge persistence | General and Deep router calls succeeded | PASS |
| `deep_web_ecosystem_tools` | Search/extract/discover web content | `web_research`, Crawl4AI | Live SearXNG search returned ranked results | PASS |
| `deep_web_advanced_tools` | Confirmed session/JavaScript extraction | Deep Web MCP | Refused without exact operator confirmation | PASS |
| `swarm_controls` | Read-only model/orchestrator status | LM Studio, orchestrator | Returned live models with no errors | PASS |
| `inline_visualizer` | Local HTML/SVG presentation | Analysis models | Local wrapper rendered; public CDN allowlist removed | PASS |
| `run_code_py` | Isolated code execution | Analysis workflows | Safe defaults confirmed; live run blocked by unavailable `unshare(2)` | DEGRADED |
| `calendar_ecosystem_tools` | Read-only calendar queries | Calendar MCP | Controlled error; Calendar MCP database password mismatch and transport mismatch | DEGRADED |
| `home_assit` | Read-only Home Assistant queries | HA MCP | Controlled missing-token response; no state-changing method | DEGRADED |
| `youtube_transcript_provider` | Transcript extraction | Tor gateway | Invalid-input guard passed; valid external transcript not proven | DEGRADED |
| `mcp_app_bridge` | Rich MCP app discovery/calls | Configured MCP server | Hardened missing-URL guard now returns a clean configuration error | DEGRADED |

Operational definitions, triggers, risks, and recipes are published in `docs/manuals/workspace_tool_operations.md` and `Knowledge - Operations - Workspace Tool Manual`.

## Verification Evidence

- Repository tests: **99 passed, 3 skipped**.
- `git diff --check`: PASS; only existing line-ending warnings.
- Open WebUI production image build: PASS.
- Deep Web MCP image build: PASS.
- Browser menu traversal: five routes returned HTTP 200 with zero console/page errors.
- Authenticated catalog reconciliation dry-run: catalog matches baseline.
- Reversible CRUD probe: five pillars PASS, no residue.
- Research persistence: PASS, retrievable Knowledge artifact.
- Container health: Open WebUI and Deep Web MCP healthy.

## Residual Risks

1. Calendar MCP cannot authenticate to `calendar-db` and exposes Streamable-HTTP while the existing Calendar tool uses an SSE client. The tool remains read-only and returns a controlled error.
2. Code Sandbox protections are intact, but this container lacks the `unshare(2)` capability required for an actual isolated execution.
3. Home Assistant requires an operator-supplied long-lived token. No state-changing method is exposed.
4. MCP Apps requires a configured MCP URL. The new guard prevents invalid-URL async cleanup failures.
5. SearXNG result relevance depends on its active engines; link verification proves reachability, not semantic relevance.

## Rollback

Use the retained containers for immediate service rollback or the sanitized/live database and catalog backups under `backups/workspace_web_search_20260612_110530` for state rollback. Stable existing entity IDs were preserved.
