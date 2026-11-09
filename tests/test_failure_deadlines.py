import asyncio
import sys
import time


from gsv_gateway.audio import AudioPipeline
from gsv_gateway.windows import LaunchSpec
from tests.conftest import ForegroundProcess
from tests.test_stream_and_queue import ASGIRequest


async def test_encoder_timeout_reports_before_upstream_drain_finishes(service):
    class NoOutput(ForegroundProcess):
        def __init__(self, spec):
            super().__init__(
                LaunchSpec(sys.executable, ["-c", "import sys;sys.stdin.buffer.read()"], spec.cwd)
            )

    def pipeline(config, executable):
        return AudioPipeline(config, executable, process_factory=NoOutput)

    service.config.value.timeouts.first_encoded_audio_seconds = 0.1
    service.scheduler.pipeline_factory = pipeline
    service.adapter.gate = asyncio.Event()
    request = ASGIRequest(service)
    start = time.monotonic()
    task = asyncio.create_task(request.run())
    try:
        await asyncio.wait_for(task, 1)
        first = request.messages.get_nowait()
        assert first["status"] == 504
        assert time.monotonic() - start < 1
        assert service.scheduler.current is not None
        assert ("a", "eof") not in service.adapter.events
    finally:
        service.adapter.gate.set()


async def test_recovery_budget_is_lifetime_cumulative(service, monkeypatch):
    service.owned = True
    service.config.value.backend.recovery.backoff_seconds = [0, 0, 0]
    calls = []

    async def stop():
        calls.append("stop")

    async def launch(*_):
        calls.append("launch")
        raise RuntimeError("failed to start")

    monkeypatch.setattr(service, "stop_backend", stop)
    monkeypatch.setattr(service, "launch_backend", launch)
    await service.recover()
    assert service.recovery_attempts == 3 and service.state == "error"
    await service.recover()
    assert calls.count("launch") == 3


async def test_storage_fault_during_recovery_does_not_restart_again(service, monkeypatch):
    service.owned = True
    service.config.value.backend.recovery.backoff_seconds = [0, 0, 0]
    count = 0

    async def stop():
        pass

    async def launch(*_):
        nonlocal count
        count += 1

    async def test(*_):
        service.state = "storage_error"
        raise OSError("disk unavailable")

    monkeypatch.setattr(service, "stop_backend", stop)
    monkeypatch.setattr(service, "launch_backend", launch)
    monkeypatch.setattr(service, "self_test", test)
    await service.recover()
    assert count == 1 and service.state == "storage_error"
