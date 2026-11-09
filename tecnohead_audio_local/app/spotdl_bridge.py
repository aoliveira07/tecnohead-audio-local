from __future__ import annotations

import argparse
import json
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
    downloader = Downloader(
        {
            "audio_providers": ["youtube-music"],
            "lyrics_providers": [],
            "output": str(args.output_dir / "{track-id}.{output-ext}"),
            "format": args.output_format,
            "bitrate": args.bitrate,
            "threads": 1,
            "scan_for_songs": False,
            "overwrite": "skip",
            "sync_without_deleting": True,
            "print_errors": True,
        }
    )
    results = downloader.download_multiple_songs(songs)
    result_payload = {
        "results": [
            {
                "song_id": song.song_id,
                "path": str(path) if path else None,
            }
            for song, path in results
        ]
    }
    temporary = args.result.with_suffix(".tmp")
    temporary.write_text(json.dumps(result_payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(args.result)


if __name__ == "__main__":
    main()
