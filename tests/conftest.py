import asyncio
import json
import math
import secrets
import struct
import subprocess

import pytest

from gsv_gateway.audio import AudioPipeline, resolve_tools
from gsv_gateway.backends.base import AudioSession
from gsv_gateway.config import LoadedConfig
from gsv_gateway.security import secure_path
from gsv_gateway.service import GatewayService


PCM = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(24000))


class ForegroundProcess:
    """Finite test subprocesses inherit the foreground console; always reaped.

    Does not exercise suspended creation/Jobs or any detached/background feature.
    """

    def __init__(self, spec):
        self.process = subprocess.Popen(
            [spec.executable, *spec.arguments],
            cwd=spec.cwd,
            env=spec.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.stdin, self.stdout, self.stderr = self.process.stdin, self.process.stdout, self.process.stderr

    @property
    def returncode(self):
        return self.process.poll()

    async def read(self, which="stdout", size=8192):
        return await asyncio.to_thread(getattr(self, which).read, size)

    async def write(self, data):
        view = memoryview(data)
        while view:
            count = await asyncio.to_thread(self.stdin.write, view)
            if not count:
                raise BrokenPipeError()
            view = view[count:]

    def end_input(self):
        if not self.stdin.closed:
            self.stdin.close()

    async def wait(self):
        return await asyncio.to_thread(self.process.wait)

    def terminate(self):
        if self.process.poll() is None:
            self.process.kill()

    def close(self):
        self.terminate()
        self.process.wait(timeout=5)
        for stream in (self.stdin, self.stdout, self.stderr):
            stream.close()


def foreground_pipeline(config, executable):
    return AudioPipeline(config, executable, process_factory=ForegroundProcess)


class FakeAdapter:
    def __init__(self, config):
        self.config = config
        self.state = "unknown_clean"
        self.loaded_voice = None
        self.events = []
        self.gate = None
        self.prepare_gate = None
        self.fail = None

    def describe(self):
        return {"id": "gpt-sovits", "object": "model", "created": 1794009600, "owned_by": "local"}

    def validate(self):
        pass

    async def probe(self):
        return "ready"

    async def prepare(self, voice, operation):
        self.events.append((voice.id, "prepare"))
        if self.prepare_gate:
            await operation.wait(self.prepare_gate.wait(), 10)
        if self.fail == "prepare":
            from gsv_gateway.errors import GatewayError

            self.state = "uncertain"
            raise GatewayError("backend_prepare_failed", "safe", uncertain=True)
        self.state, self.loaded_voice = "loaded", voice.id

    async def synthesize(self, voice, text, speed, operation):
        self.events.append((voice.id, "tts"))
        session = AudioSession(48000, None)

        async def chunks():
            session.samples += len(PCM) // 2
            session.non_silent = True
            yield PCM
            if self.gate:
                await operation.wait(self.gate.wait(), 10)
            if self.fail == "stream":
                from gsv_gateway.errors import GatewayError

                self.state = "uncertain"
                raise GatewayError("backend_stream_failed", "safe", uncertain=True)
            session.samples += len(PCM) // 2
            yield PCM
            session.complete = True
            self.events.append((voice.id, "eof"))

        session.pcm = chunks()
        return session

    def snapshot(self, voice):
        return {
            "schema_version": 1,
            "voice": voice.id,
            "registry_sha256": self.config.registry_digest,
            "backend_type": "test-only",
            "backend_payload": {"no_gsv_paths": True},
        }

    async def close(self):
        pass


@pytest.fixture
def config(tmp_path):
    token_dir = tmp_path / "secrets"
    secure_path(token_dir, directory=True)
    token = token_dir / "token.txt"
    token.write_text(secrets.token_urlsafe(32))
    secure_path(token)
    voices = {
        "schema_version": 1,
        "backend_type": "gpt_sovits",
        "assets_root": ".",
        "voices": [
            {
                "id": ident,
                "name": ident,
                "aliases": [alias],
                "declared_languages": ["zh"],
                "backend_options": {},
            }
            for ident, alias in [("a", "角色甲"), ("b", "角色乙")]
        ],
    }
    (tmp_path / "voices.jsonc").write_text(json.dumps(voices), encoding="utf-8")
    data = {
        "schema_version": 1,
        "auth": {"token_file": "secrets/token.txt"},
        "backend": {"mode": "connect"},
        "startup_test": {"voice": "a"},
        "timeouts": {
            "task_total_seconds": 20,
            "first_encoded_audio_seconds": 5,
            "downstream_stall_seconds": 2,
            "shutdown_drain_seconds": 2,
            "disconnect_drain_seconds": 2,
        },
        "queue": {"max_pending": 8, "wait_timeout_seconds": 2},
    }
    path = tmp_path / "gateway.jsonc"
    path.write_text(json.dumps(data))
    return LoadedConfig(path)


@pytest.fixture
async def service(config):
    adapter = FakeAdapter(config)
    service = GatewayService(config, adapter)
    service.ffmpeg, service.ffprobe = resolve_tools(config)
    service.token = config.read_token()
    service.state = "ready"
    service.log = lambda *args, **kwargs: None
    service.scheduler.pipeline_factory = foreground_pipeline
    service.scheduler.start()
    yield service
    await service.close()
