from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.media import playlist_text
from app.runtime import Runtime
from app.web_api import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = Runtime()
    app.state.runtime = runtime
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="TecnoHead Áudio Local", version=__version__, docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.include_router(router)


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > 1_000_000:
                return JSONResponse({"detail": "corpo JSON excede o limite de 1 MiB"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Content-Length inválido"}, status_code=400)
    return await call_next(request)


web_dir = Path(__file__).resolve().parents[1] / "web"
app.mount("/static", StaticFiles(directory=web_dir / "static"), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(web_dir / "index.html")


def _runtime_for_media(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="runtime ainda não inicializado")
    return runtime


media_app = FastAPI(title="TecnoHead Áudio Local — stream", docs_url=None, redoc_url=None, openapi_url=None)


@media_app.get("/stream/{entry_id}", include_in_schema=False)
async def stream(entry_id: str, request: Request, token: str = Query(min_length=32, max_length=128)) -> FileResponse:
    runtime = _runtime_for_media(request)
    if token != runtime.config.media.stream_token:
        raise HTTPException(status_code=404, detail="mídia não encontrada")
    track = runtime.library.get(entry_id)
    if not track or not track.relative_path:
        raise HTTPException(status_code=404, detail="mídia não encontrada")
    try:
        path = runtime.library.path_for_track(track)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="mídia não encontrada") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="mídia não encontrada")
    media_types = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac", ".wav": "audio/wav"}
    return FileResponse(path, media_type=media_types.get(path.suffix.casefold(), "application/octet-stream"), filename=path.name, content_disposition_type="inline", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@media_app.get("/playlist.m3u8", include_in_schema=False)
async def playlist(request: Request, token: str = Query(min_length=32, max_length=128), start: str | None = Query(default=None, max_length=64), shuffle: bool = False) -> PlainTextResponse:
    runtime = _runtime_for_media(request)
    if token != runtime.config.media.stream_token:
        raise HTTPException(status_code=404, detail="playlist não encontrada")
    tracks = runtime.library.ready_tracks(shuffle=shuffle)
    return PlainTextResponse(
        playlist_text(runtime.config, tracks, start_entry_id=start),
        media_type="audio/x-mpegurl",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


async def serve() -> None:
    import uvicorn

    runtime = Runtime()
    app.state.runtime = runtime
    media_app.state.runtime = runtime
    await runtime.start()
    ingress_server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=18199, lifespan="off", access_log=False, log_config=None))
    media_server = uvicorn.Server(uvicorn.Config(media_app, host="0.0.0.0", port=18198, lifespan="off", access_log=False, log_config=None))
    try:
        await asyncio.gather(ingress_server.serve(), media_server.serve())
    finally:
        await runtime.stop()


if __name__ == "__main__":
    asyncio.run(serve())

