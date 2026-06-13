// patch_frontend.mjs — retired.
//
// Catalog badges, filters, and workspace nav tweaks previously injected here
// via fragile string-anchor surgery on Svelte source files have been retired.
// Risk and dependency metadata is now communicated through native Open WebUI
// item tags (seeded by scripts/reconcile_workspace_catalog.py) which OWUI
// renders without any source patching.
//
// The CatalogBadges.svelte / CatalogFilters.svelte components in
// workspace/open-webui-overrides/src/ are preserved for reference but are no
// longer applied to the build.
//
// This file intentionally does nothing so the Dockerfile build step is a no-op
// rather than an error.
