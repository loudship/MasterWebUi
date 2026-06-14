# Open WebUI Overrides

This directory previously contained:
1. Svelte source patches (`patch_frontend.mjs` and the `src/` reference folder) to inject custom CatalogBadges and CatalogFilters.
2. Backend API custom routers (`patch_main.py` and the `backend/` folder) to mount custom catalog and research endpoints.

## Current Architecture
Both frontend and backend source patching have been completely retired:
- **Settings & Risk Posture:** Communicated through native Open WebUI item tags, seeded dynamically by the `scripts/reconcile_workspace_catalog.py` API-only script.
- **Microservices:** The catalog status API runs as a separate standalone service under `services/workspace-catalog/`.
- **gVisor Integration:** This directory only maintains the `Dockerfile` to build the hardened Open WebUI container, extending the base image and adding the gVisor `runsc` binary for the code sandbox.
