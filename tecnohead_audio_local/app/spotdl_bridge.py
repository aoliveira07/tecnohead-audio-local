from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from spotdl.download.downloader import Downloader
from spotdl.types.song import Song


def main() -> None:
    parser = argparse.ArgumentParser(description="Executa o downloader do spotDL com metadados já resolvidos")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--output-format", required=True)
    parser.add_argument("--bitrate", required=True)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    songs = [Song.from_dict(item) for item in payload.get("songs", [])]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    downloader_settings = {
        # The fallback already supplies either a YouTube URL or a ytsearch1:
        # target. Using spotDL's yt-dlp provider avoids a second Spotify/YTM
        # API lookup for the same track.
        "audio_providers": ["youtube"],
        "lyrics_providers": [],
        "output": str(args.output_dir / "{track-id}.{output-ext}"),
        "format": args.output_format,
        "bitrate": args.bitrate,
        "threads": 1,
        # The App runs without an interactive terminal. Rich's live display
        # leaks its global console between retry passes and aborts the bridge.
        "simple_tui": True,
        "scan_for_songs": False,
        "overwrite": "skip",
        "sync_without_deleting": True,
        "print_errors": True,
    }
    songs_by_id = {str(song.song_id): song for song in songs}
    pending_ids = list(songs_by_id)
    resolved: dict[str, Path] = {}
    errors: list[str] = []
    max_attempts = 3
    for attempt in range(max_attempts):
        if not pending_ids:
            break
        if attempt:
            # A short pause helps when the upstream extractor throttles a
            # burst after a large playlist.
            time.sleep(10 * attempt)
        attempt_songs = [Song.from_dict(songs_by_id[song_id].json) for song_id in pending_ids]
        downloader = Downloader(downloader_settings)
        attempt_results = downloader.download_multiple_songs(attempt_songs)
        next_pending: list[str] = []
        for song, path in attempt_results:
            song_id = str(song.song_id)
            if path is not None:
                resolved[song_id] = path
            else:
                next_pending.append(song_id)
        errors.extend(f"tentativa {attempt + 1}: {error}" for error in downloader.errors)
        pending_ids = next_pending
    result_payload = {
        "results": [
            {
                "song_id": song.song_id,
                "path": str(resolved[song.song_id]) if song.song_id in resolved else None,
            }
            for song in songs
        ],
        "errors": errors[-100:],
        "failed_song_ids": pending_ids,
    }
    temporary = args.result.with_suffix(".tmp")
    temporary.write_text(json.dumps(result_payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(args.result)


if __name__ == "__main__":
    main()
