"""Serves the People Lens web app (static files in ui/web) and tells it where the API lives.

Run: uvicorn ui.serve:app --port 8501        (API_URL env, default http://localhost:8000)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

WEB = Path(__file__).resolve().parent / "web"
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/config.js", include_in_schema=False)
def config() -> Response:
    api = os.getenv("API_URL", "http://localhost:8000")
    return Response(f"window.PEOPLELENS_API = {json.dumps(api)};", media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})


@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=WEB), name="static")
