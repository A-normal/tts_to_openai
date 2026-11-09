"""Explicit foreground GSV acceptance. Never imported by normal pytest runs.

Uses the user's already-running external GSV. It changes its loaded voice and
GSV's own configuration writeback; it never starts/stops/restarts that process.
"""

import asyncio
import json
import shutil
import subprocess
import time
import wave
from pathlib import Path

from gsv_gateway.audio import resolve_tools
from gsv_gateway.backends.base import Operation
from gsv_gateway.backends.gsv import GSVAdapter
from gsv_gateway.config import LoadedConfig, fingerprint
from gsv_gateway.windows import Mutex
from tests.conftest import foreground_pipeline


async def main():
    config = LoadedConfig(Path("config/gateway.jsonc"))
    folder = Path(".private/development/records/acceptance/2026-11-09")
    folder.mkdir(parents=True, exist_ok=True)
    template = config.resolve(config.value.backend.auto.config_template)
    backup = folder / "external-gsv-config-before.yaml"
    if backup.exists():
        raise ValueError("Acceptance evidence already exists; inspect before replay")
    shutil.copyfile(template, backup)
    record = {
        "scope": "foreground external GSV adapter + real FFmpeg; no background startup",
        "initial_config_sha256": fingerprint(template),
        "background_processes_started": False,
        "cases": [],
        "complete": False,
    }
    output = folder / "results.json"
    output.write_text(json.dumps(record, indent=2), encoding="utf-8")
    mutex = Mutex("backend-gpt-sovits-127.0.0.1-9880")
    adapter = GSVAdapter(config)
    try:
        await asyncio.to_thread(adapter.validate)
        if await adapter.probe() != "ready":
            raise ValueError("Existing GSV is not available; no process will be started")
        ffmpeg, _ = resolve_tools(config)
        voices = config.registry.voices
        cases = [(voice, 1.0) for voice in voices] + [
            (voices[0], 1.0),
            (voices[0], 1.0),
            (voices[0], 0.25),
            (voices[0], 4.0),
        ]
        for index, (voice, speed) in enumerate(cases, 1):
            case = {"case": index, "voice": voice.id, "speed": speed, "complete": False}
            record["cases"].append(case)
            output.write_text(json.dumps(record, indent=2), encoding="utf-8")
            print(json.dumps({"event": "case_start", **case}), flush=True)
            start = time.monotonic()
            operation = Operation(start + 600)
            await adapter.prepare(voice, operation)
            case["prepare_seconds"] = round(time.monotonic() - start, 3)
            synthesis_start = time.monotonic()
            session = await adapter.synthesize(
                voice, "你好，这是第一段测试。接下来检查所选角色的语音能否按顺序返回。", speed, operation
            )
            case["first_pcm_seconds"] = round(time.monotonic() - synthesis_start, 3)
            print(json.dumps({"event": "first_pcm", **case}), flush=True)
            target = folder / f"{index:02d}-{voice.id}-{speed}.flac"
            first_output = None
            with target.open("wb") as sink:

                async def emit(block):
                    nonlocal first_output
                    if first_output is None:
                        first_output = time.monotonic()
                    sink.write(block)

                pipeline = foreground_pipeline(config, ffmpeg)
                await pipeline.run(session, "flac", operation, emit)
            case["eof_seconds"] = round(time.monotonic() - synthesis_start, 3)
            case["first_encoded_seconds"] = round(first_output - synthesis_start, 3)
            decoded = await asyncio.to_thread(
                subprocess.run,
                [str(ffmpeg), "-v", "error", "-i", str(target), "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
                capture_output=True,
                timeout=30,
                check=True,
            )
            assert session.complete and session.sample_rate == 48000 and session.non_silent
            assert len(decoded.stdout) // 2 == session.samples and any(decoded.stdout)
            wav_path = target.with_suffix(".wav")
            with wave.open(str(wav_path), "wb") as wav:
                wav.setparams((1, 2, session.sample_rate, 0, "NONE", "not compressed"))
                wav.writeframes(decoded.stdout)
            case.update(
                complete=True,
                sample_rate=session.sample_rate,
                input_samples=session.samples,
                decoded_samples=len(decoded.stdout) // 2,
                audio_seconds=round(session.samples / session.sample_rate, 3),
                flac_encode_exit=0,
                flac_decode_exit=0,
                first_encoded_before_eof=case["first_encoded_seconds"] < case["eof_seconds"],
            )
            output.write_text(json.dumps(record, indent=2), encoding="utf-8")
            print(json.dumps({"event": "case_complete", **case}), flush=True)
        assert record["cases"][-2]["audio_seconds"] > record["cases"][-1]["audio_seconds"]
        record["complete"] = True
    except BaseException as error:
        record["failure_type"] = type(error).__name__
        record["failure_code"] = getattr(error, "code", "acceptance_failed")
        print(json.dumps({"event": "acceptance_failed", "code": record["failure_code"]}), flush=True)
        raise
    finally:
        record["final_config_sha256"] = fingerprint(template)
        record["last_known_voice"] = adapter.loaded_voice
        record["backend_state"] = adapter.state
        output.write_text(json.dumps(record, indent=2), encoding="utf-8")
        await adapter.close()
        mutex.close()


if __name__ == "__main__":
    asyncio.run(main())
