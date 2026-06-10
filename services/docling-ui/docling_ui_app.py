"""Docling Serve application with its native conversion workspace enabled."""

from fastapi.responses import RedirectResponse

from docling_serve.app import create_app


app = create_app()


@app.get("/", include_in_schema=False)
async def docling_workspace() -> RedirectResponse:
    """Send operators directly to the native Docling conversion interface."""
    return RedirectResponse(url="/ui/", status_code=307)
