# Hardened Open WebUI image.
#
# Previously this Dockerfile applied string-anchor patches to Svelte source
# files (patch_frontend.mjs) and to open_webui/main.py (patch_main.py) at
# build time.  Both patch files are now retired no-ops (see comments inside
# them).  The custom routers (workspace_catalog, workspace_research) run as
# separate services — not injected into this image.
#
# What this image does:
#   1. Installs gVisor (runsc) for the code-execution sandbox (blueprint §5.1).
#   2. Extends the upstream open-webui:0.9.6 image with no source mutations.

FROM open-webui:0.9.6

RUN set -eu; \
    mkdir -p /tmp/gvisor; \
    curl -fsSLo /tmp/gvisor/runsc \
        https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc; \
    curl -fsSLo /tmp/gvisor/runsc.sha512 \
        https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc.sha512; \
    cd /tmp/gvisor; \
    sha512sum -c runsc.sha512; \
    chmod 0755 runsc; \
    mv runsc /usr/local/bin/runsc; \
    rm runsc.sha512
