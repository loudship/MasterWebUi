FROM node:22-alpine3.20 AS frontend
WORKDIR /src
COPY open-webui-0.9.6/package.json open-webui-0.9.6/package-lock.json ./
RUN npm ci --force
COPY open-webui-0.9.6/ ./
COPY workspace/open-webui-overrides/src/ ./src/
COPY workspace/open-webui-overrides/patch_frontend.mjs /tmp/patch_frontend.mjs
RUN node /tmp/patch_frontend.mjs
RUN npm run build

FROM open-webui:0.9.6
COPY --from=frontend /src/build /app/build
COPY workspace/open-webui-overrides/backend/open_webui/routers/workspace_catalog.py /app/backend/open_webui/routers/workspace_catalog.py
COPY workspace/open-webui-overrides/backend/patch_main.py /tmp/patch_main.py
RUN set -eu; \
    mkdir -p /tmp/gvisor; \
    curl -fsSLo /tmp/gvisor/runsc https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc; \
    curl -fsSLo /tmp/gvisor/runsc.sha512 https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc.sha512; \
    cd /tmp/gvisor; \
    sha512sum -c runsc.sha512; \
    chmod 0755 runsc; \
    rm runsc.sha512
RUN python /tmp/patch_main.py && rm /tmp/patch_main.py
