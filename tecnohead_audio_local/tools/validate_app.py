from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.yaml"


def main() -> None:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert data["slug"] == "tecnohead_audio_local"
    assert set(data["arch"]) == {"amd64", "aarch64"}
    assert data["startup"] == "application" and data["boot"] == "auto"
    assert data["ingress"] is True and data["ingress_port"] == 18199
    assert data["panel_admin"] is True
    assert "18199/tcp" not in data.get("ports", {})
    media_maps = [item for item in data["map"] if item.get("type") == "media"]
    assert len(media_maps) == 1 and media_maps[0]["read_only"] is False and media_maps[0]["path"] == "/media"
    assert data["ports"]["18198/tcp"] == 18198
    assert data["image"] == "ghcr.io/aoliveira07/tecnohead-audio-local"
    for required in ("Dockerfile", "run.sh", "app", "web", "requirements.txt"):
        assert (ROOT / required).exists(), required
    print("Home Assistant App manifest OK")


if __name__ == "__main__":
    main()

