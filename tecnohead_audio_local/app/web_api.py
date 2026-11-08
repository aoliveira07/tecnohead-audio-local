from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.linkplay import LinkPlayError
from app.models import ConfigUpdate, CredentialUpdate, TrackStatus, public_config
from app.playback import PlaybackError
from app.runtime import Runtime
from app.settings import redact_object, redact_text
from app.sync_service import SyncBusyError

router = APIRouter(prefix="/api")


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(pattern="^(play|pause|stop|next|previous)$")


class VolumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume_percent: int = Field(ge=0, le=100)


def runtime_from(request: Request) -> Runtime:
    return request.app.state.runtime


def translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SyncBusyError):
        return HTTPException(status_code=409, detail="já existe uma sincronização em andamento")
    if isinstance(exc, (ValueError, PlaybackError)):
        return HTTPException(status_code=400, detail=redact_text(str(exc)))
    if isinstance(exc, LinkPlayError):
        return HTTPException(status_code=502, detail=redact_text(str(exc)))
    return HTTPException(status_code=500, detail="falha interna do TecnoHead Áudio Local")


def public_track(track) -> dict[str, Any]:
    data = track.model_dump(mode="json")
    data["source_url"] = redact_text(data.get("source_url") or "") if data.get("source_url") else None
    data.pop("download_relative_path", None)
    return redact_object(data)


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    runtime = runtime_from(request)
    return {"ok": True, "app_version": __import__("app").__version__, "started": runtime.started}


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    return runtime_from(request).state_payload()


@router.get("/config")
async def config(request: Request) -> dict[str, Any]:
    return runtime_from(request).public_config()


@router.put("/config")
async def save_config(payload: ConfigUpdate, request: Request) -> dict[str, Any]:
    try:
        saved = await runtime_from(request).save_public_config(payload)
        return public_config(saved)
    except Exception as exc:
        raise translate_error(exc) from exc


@router.put("/credentials")
async def save_credentials(payload: CredentialUpdate, request: Request) -> dict[str, Any]:
    try:
        runtime = runtime_from(request)
        config = runtime.config.model_copy(deep=True)
        if payload.clear:
            config.spotify_client_id = ""
            config.spotify_client_secret = ""
        if payload.spotify_client_id is not None:
            config.spotify_client_id = payload.spotify_client_id
        if payload.spotify_client_secret is not None:
            config.spotify_client_secret = payload.spotify_client_secret
        runtime.apply_config(config)
        return {
            "saved": True,
            "credentials": {
                "spotify_client_id_configured": bool(config.spotify_client_id),
                "spotify_client_secret_configured": bool(config.spotify_client_secret),
            },
        }
    except Exception as exc:
        raise translate_error(exc) from exc


@router.get("/library")
async def library(request: Request, query: str = Query(default="", max_length=200), status: str | None = Query(default=None, max_length=32)) -> dict[str, Any]:
    if status and status not in {item.value for item in TrackStatus}:
        raise HTTPException(status_code=400, detail="status de biblioteca inválido")
    items = runtime_from(request).tracks(query, status)
    return {"items": [public_track(item) for item in items], "summary": runtime_from(request).library.summary()}


@router.post("/sync")
async def sync_now(request: Request) -> dict[str, Any]:
    try:
        task = runtime_from(request).sync.start(reason="manual")
        return {"accepted": True, "status": "running", "task": task.get_name()}
    except Exception as exc:
        raise translate_error(exc) from exc


@router.post("/sync/retry/{entry_id}")
async def retry_sync(entry_id: str, request: Request) -> dict[str, Any]:
    if len(entry_id) > 64 or any(char not in "0123456789abcdef" for char in entry_id.casefold()):
        raise HTTPException(status_code=400, detail="identificador inválido")
    try:
        state = await runtime_from(request).sync.retry_track(entry_id)
        return state.model_dump(mode="json") if state else {"status": "nothing_to_retry"}
    except Exception as exc:
        raise translate_error(exc) from exc


@router.post("/sync/reprocess")
async def reprocess(request: Request) -> dict[str, Any]:
    # Reprocessing is intentionally explicit and reuses the local download
    # cache; it never calls spotDL when the source file is still present.
    try:
        runtime = runtime_from(request)
        task = runtime.sync.start(reason="reprocess")
        return {"accepted": True, "status": "running", "task": task.get_name(), "note": "faixas prontas serão processadas a partir do cache local"}
    except Exception as exc:
        raise translate_error(exc) from exc


@router.post("/playback/action")
async def playback_action(payload: ActionRequest, request: Request) -> dict[str, Any]:
    try:
        state = await runtime_from(request).playback.action(payload.action)
        return state.model_dump(mode="json")
    except Exception as exc:
        raise translate_error(exc) from exc


@router.post("/playback/volume")
async def playback_volume(payload: VolumeRequest, request: Request) -> dict[str, Any]:
    try:
        state = await runtime_from(request).playback.set_volume(payload.volume_percent)
        return state.model_dump(mode="json")
    except Exception as exc:
        raise translate_error(exc) from exc


@router.post("/diagnostics/device/{device_id}")
async def test_device(device_id: str, request: Request) -> dict[str, Any]:
    try:
        return await runtime_from(request).playback.test_device(device_id)
    except Exception as exc:
        raise translate_error(exc) from exc


@router.post("/diagnostics/group")
async def test_group(request: Request) -> dict[str, Any]:
    try:
        return await runtime_from(request).playback.test_group()
    except Exception as exc:
        raise translate_error(exc) from exc


@router.get("/diagnostics")
async def diagnostics(request: Request) -> dict[str, Any]:
    try:
        return await runtime_from(request).diagnostics()
    except Exception as exc:
        raise translate_error(exc) from exc


@router.get("/diagnostics/export")
async def diagnostics_export(request: Request) -> Response:
    try:
        runtime = runtime_from(request)
        payload = await runtime.diagnostics()
        return Response(runtime.diagnostics_service.export_json(payload), media_type="application/json", headers={"Content-Disposition": "attachment; filename=tecnohead-audio-local-diagnostics.json"})
    except Exception as exc:
        raise translate_error(exc) from exc


@router.get("/logs")
async def logs(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return {"items": runtime_from(request).events.list(limit)}

