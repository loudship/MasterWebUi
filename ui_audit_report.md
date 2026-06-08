# Open WebUI Audit Report
**Date:** 2026-06-07
**Target:** http://localhost:8080/

## 1. Executive Summary
This report summarizes the UI state and configuration verification for Open WebUI, specifically comparing the Zero-Trust security variables defined in the backend `.env` configuration against the exposed UI state.

An automated audit was performed via the backend APIs (`/api/v1/auths/admin/config`, `/api/v1/configs/connections`, and `/api/v1/configs/export`) using an administratively generated JWT token to inspect the active configuration values the WebUI presents and operates under.

## 2. Zero-Trust Security Variable Verification

Below is the verification of each specified `.env` zero-trust variable against the current UI state:

| Variable | `.env` Expected State | Active UI State | Status | Notes |
|---|---|---|---|---|
| `ENABLE_PERSISTENT_CONFIG` | `False` | *Not directly visible* | **N/A** | This is a backend-level setting. The UI does not expose a toggle for this variable, meaning it fails to reflect this specific backend state to the admin. |
| `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` | `False` | *Not directly visible* | **N/A** | There is no UI element in the Code Execution or Admin settings that reflects or controls this variable. |
| `ENABLE_SIGNUP` | `False` | `True` | **Mismatch** | The admin authentication configuration API (`/api/v1/auths/admin/config`) actively reports `ENABLE_SIGNUP: true`, directly contradicting the `.env` state. |
| `DEFAULT_USER_ROLE` | `pending` | `admin` | **Mismatch** | The admin authentication configuration actively reports `DEFAULT_USER_ROLE: "admin"`, contradicting the `.env` state which demands `pending`. |
| `ENABLE_OPENAI_API_PASSTHROUGH` | `False` | *Not directly visible* | **N/A** | The UI does not provide a toggle or status indicator for OpenAI API passthrough, failing to reflect this backend state. |
| `ENABLE_DIRECT_CONNECTIONS` | `False` | `False` (Connections) / `True` (Export) | **Partial Match** | The connections configuration endpoint (`/api/v1/configs/connections`) reports `ENABLE_DIRECT_CONNECTIONS: false` (matching), but the bulk export API payload reflects `"direct": {"enable": true}`. |
| `ENV` | `prod` | *Not directly visible* | **N/A** | The deployment environment variable (`prod`) is not exposed anywhere in the UI configuration. |

## 3. Findings & Conclusion
- **Critical Security Mismatches:** Both `ENABLE_SIGNUP` and `DEFAULT_USER_ROLE` present significant security risks, as the UI actively overrides the zero-trust `.env` baseline to permit signups and default new users to an `admin` role.
- **Missing UI Reflections:** Variables like `ENABLE_OPENAI_API_PASSTHROUGH`, `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS`, and `ENABLE_PERSISTENT_CONFIG` are completely unrepresented in the Admin UI settings.
- **API Discrepancies:** `ENABLE_DIRECT_CONNECTIONS` has conflicting states between different admin APIs (export vs connections config), which may lead to unpredictable UI behavior.

These configurations need immediate remediation to align the application's persistent database state with the `.env` zero-trust architecture.
