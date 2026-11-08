from __future__ import annotations

import json
import subprocess

import pytest

from app.loudness import LoudnessError, LoudnessProcessor, parse_loudnorm_json


def test_parse_loudnorm_json_from_ffmpeg_output():
    metrics = parse_loudnorm_json('noise\n{"input_i":"-20.1","input_lra":"4.2","input_tp":"-1.3","input_thresh":"-30.0","target_offset":"2.1"}\n')
    assert metrics.input_i == -20.1
    assert metrics.input_lra == 4.2
    assert metrics.target_offset == 2.1


def test_parse_loudnorm_rejects_missing_metrics():
    with pytest.raises(LoudnessError):
        parse_loudnorm_json('{"input_i": -18}')


class FakeFFmpeg:
    def __init__(self, fail_second=False):
        self.commands = []
        self.fail_second = fail_second

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"streams": [{"codec_type": "audio", "codec_name": "mp3"}], "format": {"duration": "120", "format_name": "mp3"}}), stderr="")
        if "-f" in command and "null" in command:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr='{"input_i":"-20","input_lra":"5","input_tp":"-1","input_thresh":"-30","target_offset":"2"}')
        if self.fail_second:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="simulated ffmpeg error token=secret")
        with open(command[-1], "wb") as handle:
            handle.write(b"normalized")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


@pytest.mark.asyncio
async def test_two_pass_normalization_is_atomic(paths, config):
    source = paths.downloads / "source.mp3"
    source.write_bytes(b"source")
    destination = paths.library / "tracks" / "out.mp3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    runner = FakeFFmpeg()
    result = await LoudnessProcessor(ffmpeg="ffmpeg", ffprobe="ffprobe", runner=runner).process(source, destination, config.normalization)
    assert result.result.mode == "two_pass_linear"
    assert destination.read_bytes() == b"normalized"
    assert sum("-af" in command for command in runner.commands) == 2


@pytest.mark.asyncio
async def test_second_pass_failure_does_not_replace_ready_file(paths, config):
    source = paths.downloads / "source.mp3"
    source.write_bytes(b"source")
    destination = paths.library / "tracks" / "out.mp3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"previous-ready")
    with pytest.raises(LoudnessError):
        await LoudnessProcessor(runner=FakeFFmpeg(fail_second=True)).process(source, destination, config.normalization)
    assert destination.read_bytes() == b"previous-ready"

