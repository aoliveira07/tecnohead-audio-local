from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.models import NormalizationConfig, NormalizationResult
from app.settings import redact_text

LOGGER = logging.getLogger("tecnohead_audio_local.loudness")


class LoudnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeInfo:
    duration_seconds: float
    codec: str
    audio_streams: int
    format_name: str


@dataclass(frozen=True)
class LoudnessMetrics:
    input_i: float | None
    input_lra: float | None
    input_tp: float | None
    input_thresh: float | None
    target_offset: float | None


@dataclass(frozen=True)
class NormalizationOutput:
    result: NormalizationResult
    probe: ProbeInfo


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_loudnorm_json(output: str) -> LoudnessMetrics:
    """Parse the JSON object emitted by FFmpeg loudnorm without trusting logs."""

    decoder = json.JSONDecoder()
    candidates: list[dict[str, object]] = []
    for match in re.finditer(r"\{", output or ""):
        try:
            value, _ = decoder.raw_decode(output[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and {"input_i", "input_lra", "input_tp", "input_thresh", "target_offset"}.intersection(value):
            candidates.append(value)
    if not candidates:
        raise LoudnessError("FFmpeg não devolveu métricas JSON do loudnorm")
    value = candidates[-1]
    metrics = LoudnessMetrics(
        input_i=_number(value.get("input_i")),
        input_lra=_number(value.get("input_lra")),
        input_tp=_number(value.get("input_tp")),
        input_thresh=_number(value.get("input_thresh")),
        target_offset=_number(value.get("target_offset")),
    )
    if metrics.input_i is None or metrics.input_thresh is None or metrics.target_offset is None:
        raise LoudnessError("métricas essenciais do loudnorm não são numéricas")
    return metrics


def _audio_codec_args(output_format: str, bitrate: str) -> list[str]:
    if output_format == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", bitrate]
    if output_format == "m4a":
        return ["-c:a", "aac", "-b:a", bitrate]
    if output_format == "ogg":
        return ["-c:a", "libvorbis", "-q:a", "5"]
    if output_format == "opus":
        return ["-c:a", "libopus", "-b:a", bitrate]
    if output_format == "flac":
        return ["-c:a", "flac"]
    if output_format == "wav":
        return ["-c:a", "pcm_s16le"]
    raise LoudnessError(f"formato de saída não suportado: {output_format}")


class LoudnessProcessor:
    def __init__(self, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", runner: Callable[..., subprocess.CompletedProcess[str]] | None = None):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.runner = runner or self._default_runner

    @staticmethod
    def _default_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, text=True, capture_output=True, check=False, **kwargs)

    def available(self) -> bool:
        return bool(shutil.which(self.ffmpeg) and shutil.which(self.ffprobe))

    def probe(self, source: Path) -> ProbeInfo:
        if not source.is_file() or source.stat().st_size <= 0:
            raise LoudnessError("arquivo de áudio inexistente ou vazio")
        command = [
            self.ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:stream=codec_type,codec_name",
            "-of",
            "json",
            str(source),
        ]
        completed = self.runner(command)
        if completed.returncode != 0:
            raise LoudnessError(f"ffprobe falhou: {redact_text(completed.stderr or '')}")
        try:
            payload = json.loads(completed.stdout or "{}")
            streams = [item for item in payload.get("streams", []) if item.get("codec_type") == "audio"]
            duration = float(payload.get("format", {}).get("duration", 0))
            codec = str(streams[0].get("codec_name", "")) if streams else ""
            format_name = str(payload.get("format", {}).get("format_name", ""))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise LoudnessError("ffprobe devolveu JSON inválido") from exc
        if len(streams) != 1 or duration <= 0 or not codec:
            raise LoudnessError("arquivo não possui exatamente um stream de áudio válido")
        return ProbeInfo(duration_seconds=duration, codec=codec, audio_streams=len(streams), format_name=format_name)

    def first_pass_command(self, source: Path, config: NormalizationConfig) -> list[str]:
        filter_value = f"loudnorm=I={config.target_lufs}:TP={config.true_peak_db}:LRA={config.lra}:print_format=json"
        return [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-af",
            filter_value,
            "-f",
            "null",
            os.devnull,
        ]

    def second_pass_command(self, source: Path, target: Path, config: NormalizationConfig, metrics: LoudnessMetrics) -> tuple[list[str], str]:
        linear = bool(metrics.input_lra is not None and metrics.input_lra > 0 and metrics.input_tp is not None)
        parts = [f"I={config.target_lufs}", f"TP={config.true_peak_db}", f"LRA={config.lra}"]
        if linear:
            parts.extend(
                [
                    f"measured_I={metrics.input_i}",
                    f"measured_LRA={metrics.input_lra}",
                    f"measured_TP={metrics.input_tp}",
                    f"measured_thresh={metrics.input_thresh}",
                    f"offset={metrics.target_offset}",
                    "linear=true",
                ]
            )
            mode = "two_pass_linear"
        else:
            parts.append("linear=false")
            mode = "two_pass_dynamic_fallback"
        parts.append("print_format=summary")
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-af",
            "loudnorm=" + ":".join(parts),
            *_audio_codec_args(config.output_format, config.bitrate),
            "-map_metadata",
            "0",
            str(target),
        ]
        return command, mode

    async def process(self, source: Path, destination: Path, config: NormalizationConfig) -> NormalizationOutput:
        if not config.enabled:
            probe = await asyncio.to_thread(self.probe, source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f"{destination.stem}.", suffix=destination.suffix, dir=destination.parent)
            os.close(fd)
            try:
                shutil.copyfile(source, temp_name)
                os.replace(temp_name, destination)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return NormalizationOutput(
                result=NormalizationResult(
                    mode="disabled",
                    target_lufs=config.target_lufs,
                    true_peak_db=config.true_peak_db,
                    duration_seconds=probe.duration_seconds,
                    output_format=config.output_format,
                    completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
                probe=probe,
            )

        source_probe = await asyncio.to_thread(self.probe, source)
        first = await asyncio.to_thread(self.runner, self.first_pass_command(source, config))
        if first.returncode != 0:
            raise LoudnessError(f"primeira passagem loudnorm falhou: {redact_text(first.stderr or '')}")
        metrics = parse_loudnorm_json(f"{first.stdout or ''}\n{first.stderr or ''}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f"{destination.stem}.", suffix=destination.suffix, dir=destination.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            command, mode = self.second_pass_command(source, temp_path, config, metrics)
            second = await asyncio.to_thread(self.runner, command)
            if second.returncode != 0:
                raise LoudnessError(f"segunda passagem loudnorm falhou: {redact_text(second.stderr or '')}")
            output_probe = await asyncio.to_thread(self.probe, temp_path)
            if abs(output_probe.duration_seconds - source_probe.duration_seconds) > max(1.0, source_probe.duration_seconds * 0.02):
                raise LoudnessError("duração do arquivo normalizado diverge da origem")
            os.replace(temp_path, destination)
            result = NormalizationResult(
                mode=mode,
                target_lufs=config.target_lufs,
                true_peak_db=config.true_peak_db,
                input_lufs=metrics.input_i,
                input_lra=metrics.input_lra,
                input_true_peak_db=metrics.input_tp,
                input_threshold=metrics.input_thresh,
                target_offset=metrics.target_offset,
                duration_seconds=output_probe.duration_seconds,
                output_format=config.output_format,
                completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            return NormalizationOutput(result=result, probe=output_probe)
        finally:
            if temp_path.exists():
                temp_path.unlink()

