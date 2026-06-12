# Workspace Tool Operations Manual

## Operating Rules

- Prefer the narrowest read-only tool that can answer the request.
- Use state-changing capabilities only when explicitly enabled and confirmed. Calendar, Home Assistant, and Orchestrator tools are intentionally read-only.
- Treat network content as untrusted. Report empty payloads, unavailable dependencies, and failed links instead of inventing results.
- Use bounded concurrency and timeouts. Do not retry indefinitely.

## Tool Catalog

| Tool | Use case | Invoke when | Synergies | Safety posture |
|---|---|---|---|---|
| Tool - Research - General and Deep Web | Current facts, verified links, investigative reports | The answer depends on live or external information | Knowledge reports, Evidence-Backed Web skill | Read-only, bounded parallelism |
| Tool - Deep Web MCP - Search and Extract | Direct URL extraction and specialized database search | A known page or configured database must be read | Web Research Router | Read-only |
| Tool - Deep Web MCP - Advanced Session and JavaScript | Session-backed or explicit JavaScript extraction | A read-only extraction requires authenticated state or JavaScript | Deep Web Search and Extract | Operator confirmation required |
| Tool - Code Sandbox - Python and Bash | Reproducible local calculations and scripts | Calculation or transformation is safer than mental arithmetic | Inline Visualizer | gVisor, no networking/install/update |
| Tool - Visualization - Inline HTML and SVG | Local visual artifacts | The user explicitly requests a visualization | Code Sandbox, Data Analysis skill | Local-only resources |
| Tool - Media - YouTube Transcript via Tor | Chronological video transcript extraction | A YouTube URL or ID is provided | Web Research Router | External network through Tor |
| Tool - Calendar - Read-Only Fallback | List calendars and events | The user asks about calendar state | General assistant | Read-only |
| Tool - Home Assistant - Read-Only Fallback | Inspect entity state and domains | The user asks about Home Assistant state | Local diagnostics | Read-only |
| Tool - Orchestrator - Read-Only Status | Inspect orchestrator and loaded model health | Diagnosing local agent/model readiness | Safe Local Diagnostics skill | Read-only |
| Tool - MCP Apps - Rich UI Bridge | Discover and invoke configured MCP tools | A matching MCP capability is required | Any MCP-backed workflow | Review discovered tool risk before invocation |

## Research Strategies

### General Search

Use `research_web` with `strategy="general"` for immediate factual lookup. The router performs one SearXNG search, validates links concurrently, and returns titles, domains, summaries, and active Markdown links.

### Deep Research

Use `research_web` with `strategy="deep"` for comparisons, investigations, evidence reports, or explicit deep-research requests. The router performs an initial search, targeted coverage-gap searches, bounded parallel extraction, deduplication, link validation, and a complete Markdown artifact.

## Recipes

### Current Fact With Evidence

1. Run General Search.
2. Prefer active primary-source links.
3. Return a concise answer with clickable Markdown references.

### Investigative Report

1. Run Deep Research.
2. Review failed or unavailable sources explicitly.
3. Persist the complete report to `Knowledge - Research - Web Search Reports`.
4. Return a bounded synthesis with indexed links.

### Data-Backed Visualization

1. Use Web Research only if current external data is required.
2. Use Code Sandbox for reproducible calculation and validation.
3. Use Inline Visualizer only after an explicit visualization request.

### Local Service Diagnosis

1. Use Orchestrator Status and other read-only health tools.
2. Inspect logs and configuration.
3. Propose the smallest remediation; do not mutate services without explicit operator authorization.
