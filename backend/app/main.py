from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .config import build_default_council_config
from .events import EventBroker
from .google_gemini import validate_google_gemini_config
from .runtime import CouncilRuntime
from .schemas import (
    CancelRunResponse,
    ConfigListResponse,
    CouncilConfig,
    CreateRunRequest,
    RunListResponse,
    RunSnapshot,
)
from .storage import SQLiteStorage
from .settings import get_settings


DATABASE_PATH = Path(__file__).resolve().parent.parent / "data" / "council.sqlite3"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    storage = SQLiteStorage(DATABASE_PATH)
    storage.initialize()
    default_config = build_default_council_config(settings)
    validate_google_gemini_config(settings, default_config)
    storage.upsert_config(default_config)
    broker = EventBroker()
    runtime = CouncilRuntime(storage=storage, broker=broker, settings=settings)
    app.state.storage = storage
    app.state.broker = broker
    app.state.runtime = runtime
    app.state.default_config_id = default_config.id
    app.state.settings = settings
    yield


app = FastAPI(title="llm-council-workflow backend", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_storage(app_: FastAPI) -> SQLiteStorage:
    return app_.state.storage


def get_runtime(app_: FastAPI) -> CouncilRuntime:
    return app_.state.runtime


def _encode_sse(event: dict[str, str]) -> str:
    lines = []
    if "id" in event:
        lines.append(f"id: {event['id']}")
    if "event" in event:
        lines.append(f"event: {event['event']}")
    if "data" in event:
        for line in event["data"].splitlines():
            lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/configs", response_model=ConfigListResponse)
async def list_configs() -> ConfigListResponse:
    configs = await asyncio.to_thread(get_storage(app).list_configs)
    return ConfigListResponse(items=configs)


@app.get("/api/configs/{config_id}", response_model=CouncilConfig)
async def get_config(config_id: str) -> CouncilConfig:
    config = await asyncio.to_thread(get_storage(app).get_config, config_id)
    if not config:
        raise HTTPException(status_code=404, detail="Config not found.")
    return config


@app.post("/api/configs", response_model=CouncilConfig)
async def create_config(config: CouncilConfig) -> CouncilConfig:
    existing = await asyncio.to_thread(get_storage(app).get_config, config.id)
    if existing:
        raise HTTPException(status_code=409, detail="Config id already exists.")
    return await asyncio.to_thread(get_storage(app).upsert_config, config)


@app.put("/api/configs/{config_id}", response_model=CouncilConfig)
async def update_config(config_id: str, config: CouncilConfig) -> CouncilConfig:
    if config.id != config_id:
        config = config.model_copy(update={"id": config_id})
    return await asyncio.to_thread(get_storage(app).upsert_config, config)


@app.delete("/api/configs/{config_id}")
async def delete_config(config_id: str) -> dict[str, bool]:
    deleted = await asyncio.to_thread(get_storage(app).delete_config, config_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Config not found.")
    return {"deleted": True}


@app.post("/api/runs", response_model=RunSnapshot)
async def create_run(request: CreateRunRequest) -> RunSnapshot:
    config = request.config
    if not config:
        if request.config_id:
            config = await asyncio.to_thread(get_storage(app).get_config, request.config_id)
        else:
            config = await asyncio.to_thread(
                get_storage(app).get_config, app.state.default_config_id
            )
    if not config:
        raise HTTPException(status_code=404, detail="Config not found.")
    try:
        validate_google_gemini_config(app.state.settings, config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await get_runtime(app).start_run(request.query, config)


@app.get("/api/runs", response_model=RunListResponse)
async def list_runs(limit: int = Query(default=50, ge=1, le=200)) -> RunListResponse:
    items = await asyncio.to_thread(get_storage(app).list_runs, limit)
    return RunListResponse(items=items)


@app.delete("/api/runs")
async def clear_runs() -> dict[str, int]:
    deleted = await asyncio.to_thread(get_storage(app).clear_run_history)
    return {"deleted": deleted}


@app.get("/api/runs/{run_id}", response_model=RunSnapshot)
async def get_run(run_id: str) -> RunSnapshot:
    snapshot = await asyncio.to_thread(get_storage(app).get_run, run_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Run not found.")
    return snapshot


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str) -> dict[str, bool]:
    try:
        deleted = await asyncio.to_thread(get_storage(app).delete_run, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"deleted": True}


@app.get("/api/runs/{run_id}/events")
async def stream_run_events(run_id: str, after_id: int = Query(default=0, ge=0)):
    storage = get_storage(app)
    broker = app.state.broker
    snapshot = await asyncio.to_thread(storage.get_run, run_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Run not found.")

    async def event_generator():
        historical_events = await asyncio.to_thread(storage.list_events, run_id, after_id)
        for event in historical_events:
            yield _encode_sse(
                {
                    "id": str(event.id or ""),
                    "event": event.type,
                    "data": event.model_dump_json(),
                }
            )
        queue = await broker.subscribe(run_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _encode_sse(
                        {
                            "id": str(event.id or ""),
                            "event": event.type,
                            "data": event.model_dump_json(),
                        }
                    )
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            await broker.unsubscribe(run_id, queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/cancel", response_model=CancelRunResponse)
async def cancel_run(run_id: str) -> CancelRunResponse:
    snapshot = await get_runtime(app).cancel_run(run_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Run not found.")
    return CancelRunResponse(run_id=run_id, status=snapshot.status)
