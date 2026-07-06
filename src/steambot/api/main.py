"""SteamBot FastAPI application.

Serves the picks API + minimal Jinja2 UI for the HITL approval workflow.
The httpx.AsyncClient and LangGraph checkpointer are shared across requests
via the lifespan context so connections are pooled.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from steambot.graph import build_graph

_http_client: httpx.AsyncClient | None = None
_graph = None
_templates: Jinja2Templates | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _graph
    _http_client = httpx.AsyncClient()
    _graph = build_graph(_http_client)
    yield
    await _http_client.aclose()
    _http_client = None
    _graph = None


app = FastAPI(title="SteamBot", version="0.1.0", lifespan=lifespan)


def get_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized -- is the app running?")
    return _http_client


def get_graph():
    if _graph is None:
        raise RuntimeError("Graph not initialized -- is the app running?")
    return _graph


from steambot.api import routes  # noqa: E402 -- import after app is defined

app.include_router(routes.router)
