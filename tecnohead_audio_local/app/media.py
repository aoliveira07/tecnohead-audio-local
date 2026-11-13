from __future__ import annotations

from urllib.parse import urlencode, urlsplit, urlunsplit

from app.models import AppConfig, Track


def media_base_url(config: AppConfig) -> str:
    base_url = config.media.base_url.rstrip("/")
    if not base_url:
        raise ValueError("configure a URL base de mídia acessível pelos AudioCast")
    parsed = urlsplit(base_url)
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise ValueError("URL base de mídia contém uma porta inválida") from exc
    if explicit_port is None:
        base_url = urlunsplit(
            (
                parsed.scheme,
                f"{parsed.netloc}:{config.media.port}",
                parsed.path.rstrip("/"),
                "",
                "",
            )
        )
    return base_url


def media_url(config: AppConfig, entry_id: str) -> str:
    return f"{media_base_url(config)}/stream/{entry_id}?{urlencode({'token': config.media.stream_token})}"


def playlist_url(config: AppConfig, *, start_entry_id: str | None = None, shuffle: bool = False) -> str:
    query: dict[str, str] = {"token": config.media.stream_token}
    if start_entry_id:
        query["start"] = start_entry_id
    if shuffle:
        query["shuffle"] = "1"
    return f"{media_base_url(config)}/playlist.m3u8?{urlencode(query)}"


def _m3u_text(value: str) -> str:
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


def playlist_text(config: AppConfig, tracks: list[Track], *, start_entry_id: str | None = None) -> str:
    ordered = list(tracks)
    if start_entry_id:
        start_index = next((index for index, track in enumerate(ordered) if track.entry_id == start_entry_id), 0)
        ordered = ordered[start_index:] + ordered[:start_index]
    lines = ["#EXTM3U"]
    for track in ordered:
        title = f"{_m3u_text(track.artist)} - {_m3u_text(track.title)}".strip(" -")
        lines.append(f"#EXTINF:{int(track.duration_seconds or -1)},{title}")
        lines.append(media_url(config, track.entry_id))
    return "\n".join(lines) + "\n"
