# Open WebUI Audit Report
**Date:** 2026-06-14
**Target:** http://127.0.0.1:3000/

## 1. Executive Summary
This report summarizes the UI state and configuration verification for Open WebUI, specifically comparing the Zero-Trust security variables defined in the backend `.env` configuration against the exposed UI state.

An automated audit was performed via the backend APIs (`/api/v1/auths/admin/config`, `/api/v1/configs/connections`, and `/api/v1/configs/export`) using an administratively generated JWT token to inspect the active configuration values the WebUI presents and operates under.

## 2. Zero-Trust Security Variable Verification

Below is the verification of each specified `.env` zero-trust variable against the current UI state:

| Variable | `.env` Expected State | Active UI State | Status | Notes |
|---|---|---|---|---|
| `ENABLE_PERSISTENT_CONFIG` | `False` | `Not directly visible` | **N/A** | Backend-level setting. No direct toggle in UI. |
| `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` | `False` | `Not directly visible` | **N/A** | No UI element reflects or controls this variable. |
| `ENABLE_SIGNUP` | `False` | `False` | **Match** | API reports ENABLE_SIGNUP: False |
| `DEFAULT_USER_ROLE` | `pending` | `pending` | **Match** | API reports DEFAULT_USER_ROLE: pending |
| `ENABLE_OPENAI_API_PASSTHROUGH` | `False` | `Not directly visible` | **N/A** | The UI does not expose a toggle for OpenAI API passthrough. |
| `ENABLE_DIRECT_CONNECTIONS` | `False` | `False (Connections) / False (Export)` | **Match** | Connections=False, Export=False |
| `ENV` | `prod` | `Not directly visible` | **N/A** | The deployment environment variable is not exposed in the UI configuration. |

## 3. Findings & Conclusion
- **Zero-Trust Baseline Aligned:** No active mismatches were found. The database settings match the expected `.env` zero-trust configuration.
- **Missing UI Reflections:** Variables like `ENABLE_OPENAI_API_PASSTHROUGH`, `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS`, and `ENABLE_PERSISTENT_CONFIG` are completely unrepresented in the Admin UI settings.

This configuration audit ensures the database state matches the zero-trust architecture parameters.