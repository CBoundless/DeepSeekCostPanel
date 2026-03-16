from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .core.settings import FRONTEND_ROOT
from .db import init_db
from .services.trading_runtime import runtime_manager


app = FastAPI(title="DeepSeekCostPanel Admin", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    runtime_manager.initialize()


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/app/")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


if FRONTEND_ROOT.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_ROOT), html=True), name="frontend")
