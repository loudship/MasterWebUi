from pathlib import Path

main_path = Path("/app/backend/open_webui/main.py")
text = main_path.read_text(encoding="utf-8")

import_anchor = "    utils,\n)"
if "workspace_catalog," not in text:
    text = text.replace(import_anchor, "    utils,\n    workspace_catalog,\n)")

route_anchor = "app.include_router(skills.router, prefix='/api/v1/skills', tags=['skills'])"
route = "app.include_router(workspace_catalog.router, prefix='/api/v1/workspace/catalog', tags=['workspace-catalog'])"
if route not in text:
    text = text.replace(route_anchor, f"{route_anchor}\n{route}")

main_path.write_text(text, encoding="utf-8")
