import asyncio
import json
import subprocess

import pytest

from gsv_gateway.api import create_app
from gsv_gateway.errors import GatewayError


class ASGIRequest:
    def __init__(self, service, voice="a", fmt="mp3"):
        self.service = service
        self.incoming = asyncio.Queue()
        self.messages = asyncio.Queue()
        self.body = json.dumps(
            {"model": "gpt-sovits", "voice": voice, "input": "test", "response_format": fmt}
        ).encode()

    async def run(self):
        await self.incoming.put({"type": "http.request", "body": self.body, "more_body": False})
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/audio/speech",
            "raw_path": b"/v1/audio/speech",
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
            "headers": [
                (b"authorization", b"Bearer " + self.service.tokens[0]),
                (b"content-type", b"application/json"),
            ],
        }
        await create_app(self.service)(scope, self.incoming.get, self.messages.put)

    async def first_payload(self):
        async with asyncio.timeout(10):
            start = await self.messages.get()
            assert start["type"] == "http.response.start" and start["status"] == 200
            while True:
                message = await self.messages.get()
                if message.get("body"):
                    return message["body"]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("fmt", ["mp3", "opus", "aac", "flac", "wav", "pcm"])
async def test_real_encoder_emits_decodable_prefix_before_later_pcm(service, fmt, native, monkeypatch):
    if native:
        import win32process
        from gsv_gateway.audio import AudioPipeline

        monkeypatch.setattr(win32process, "CREATE_NO_WINDOW", 0)
        service.scheduler.pipeline_factory = AudioPipeline
    service.adapter.gate = asyncio.Event()
    request = ASGIRequest(service, fmt=fmt)
    task = asyncio.create_task(request.run())
    try:
        prefix = await request.first_payload()
        assert not task.done()
        assert ("a", "eof") not in service.adapter.events
        args = ["-f", "s16le", "-ar", "24000", "-ac", "1"] if fmt == "pcm" else []
        decoded = await asyncio.to_thread(
            subprocess.run,
            [
                str(service.ffmpeg),
                "-v",
                "error",
                "-probesize",
                "32",
                "-analyzeduration",
                "1",
                *args,
                "-i",
                "pipe:0",
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "pipe:1",
            ],
            input=prefix,
            capture_output=True,
            timeout=5,
        )
        assert any(decoded.stdout), (fmt, decoded.stderr.decode(errors="replace"))
    finally:
        service.adapter.gate.set()
        await asyncio.wait_for(task, 5)


async def test_disconnect_does_not_release_ownership(service):
    service.adapter.gate = asyncio.Event()
    first = ASGIRequest(service)
    task_a = asyncio.create_task(first.run())
    await first.first_payload()
    await first.incoming.put({"type": "http.disconnect"})
    await asyncio.wait_for(task_a, 2)
    second = ASGIRequest(service, voice="b")
    task_b = asyncio.create_task(second.run())
    await asyncio.sleep(0.05)
    assert ("b", "prepare") not in service.adapter.events
    assert not service.config.snapshot_path.exists()
    service.adapter.gate.set()
    await asyncio.wait_for(task_b, 5)
    assert service.adapter.events.index(("a", "eof")) < service.adapter.events.index(("b", "prepare"))


async def test_disconnect_during_prepare_is_owned_until_setters_finish(service):
    service.adapter.prepare_gate = asyncio.Event()
    first = ASGIRequest(service)
    task_a = asyncio.create_task(first.run())
    async with asyncio.timeout(2):
        while not service.adapter.events:
            await asyncio.sleep(0)
    await first.incoming.put({"type": "http.disconnect"})
    await task_a
    second = ASGIRequest(service, voice="b")
    task_b = asyncio.create_task(second.run())
    await asyncio.sleep(0.05)
    assert ("b", "prepare") not in service.adapter.events
    service.adapter.prepare_gate.set()
    await asyncio.wait_for(task_b, 5)
    assert ("a", "tts") not in service.adapter.events


async def test_queue_capacity_and_cancelled_jobs_never_touch_backend(service):
    service.adapter.gate = asyncio.Event()
    active = ASGIRequest(service)
    running = asyncio.create_task(active.run())
    await active.first_payload()
    pending = [
        service.scheduler.submit(service.config.registry.find("b"), "test", 1, "mp3", str(i))
        for i in range(8)
    ]
    with pytest.raises(GatewayError) as error:
        service.scheduler.submit(service.config.registry.find("b"), "test", 1, "mp3", "overflow")
    assert error.value.status == 429
    for job in pending:
        job.cancelled.set()
    service.adapter.gate.set()
    await asyncio.wait_for(running, 5)
    await asyncio.sleep(0.05)
    assert ("b", "prepare") not in service.adapter.events


async def test_upstream_failure_after_audio_aborts_and_preserves_last_good(service):
    first = ASGIRequest(service)
    await first.run()
    async with asyncio.timeout(2):
        while service.scheduler.current:
            await asyncio.sleep(0)
    before = service.config.snapshot_path.read_bytes()
    service.adapter.fail = "stream"
    service.adapter.gate = asyncio.Event()
    second = ASGIRequest(service, voice="b")
    task = asyncio.create_task(second.run())
    await second.first_payload()
    service.adapter.gate.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(task, 5)
    async with asyncio.timeout(2):
        while service.scheduler.current:
            await asyncio.sleep(0)
    assert service.config.snapshot_path.read_bytes() == before
    assert service.state == "manual_recovery_required"
    messages = []
    while not second.messages.empty():
        messages.append(second.messages.get_nowait())
    assert not any(m.get("more_body") is False for m in messages)
    assert not any(b'"error"' in m.get("body", b"") for m in messages)


async def test_snapshot_failure_after_delivery_does_not_rewrite_http(service, monkeypatch):
    def broken(_):
        raise OSError("private marker must not escape")

    monkeypatch.setattr(service.store, "commit", broken)
    request = ASGIRequest(service)
    await asyncio.wait_for(request.run(), 5)
    async with asyncio.timeout(2):
        while service.scheduler.current:
            await asyncio.sleep(0)
    messages = []
    while not request.messages.empty():
        messages.append(request.messages.get_nowait())
    assert messages[0]["status"] == 200
    assert messages[-1]["more_body"] is False
    assert service.state == "storage_error"
