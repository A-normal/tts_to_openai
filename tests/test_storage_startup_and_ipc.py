import asyncio
import os
import time
import uuid

import pytest

from tests.conftest import ForegroundProcess, foreground_pipeline
from gsv_gateway import audio
from gsv_gateway import service as service_module
from gsv_gateway.cli import main
from gsv_gateway.config import control_state_path
from gsv_gateway.errors import GatewayError
from gsv_gateway.ipc import ControlServer, control_request
from gsv_gateway.security import secure_path
from gsv_gateway.storage import atomic_write, write_state
from gsv_gateway.windows import current_creation_time


def test_atomic_snapshot_replace_failure_preserves_previous(config, monkeypatch):
    path = config.snapshot_path
    atomic_write(path, b"previous")

    def fail(*_):
        raise OSError("storage failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(path, b"candidate")
    assert path.read_bytes() == b"previous"
    assert not list(path.parent.glob(".pending-*"))


async def test_startup_real_flac_roundtrip_and_snapshot_selection(service, monkeypatch):
    monkeypatch.setattr(service_module, "AudioPipeline", foreground_pipeline)
    monkeypatch.setattr(audio, "ManagedProcess", ForegroundProcess)
    secure_path(service.config.state_dir, directory=True)
    service.configure_logs()
    voice, source = service.store.select()
    assert voice.id == "a" and source == "startup_voice"
    await service.self_test(voice, source, time.monotonic() + 10)
    before = service.config.snapshot_path.read_bytes()
    service.store.commit(service.config.registry.find("b"))
    selected, source = service.store.select()
    assert selected.id == "b" and source == "last_good"
    assert service.config.snapshot_path.read_bytes() != before
    assert not list(service.config.state_dir.glob("*.flac"))
    log = (service.config.resolve(service.config.value.logging.directory) / "startup.log").read_text(
        encoding="utf-8"
    )
    assert "startup_test_success" in log and '"decoder_exit": 0' in log
    assert service.tokens[0].decode("ascii") not in log and service.config.value.startup_test.text not in log


async def test_startup_storage_failure_does_not_trigger_backend_restart(service, monkeypatch):
    monkeypatch.setattr(service_module, "AudioPipeline", foreground_pipeline)
    monkeypatch.setattr(audio, "ManagedProcess", ForegroundProcess)
    secure_path(service.config.state_dir, directory=True)
    service.configure_logs()

    def fail(_):
        raise OSError("disk failure")

    monkeypatch.setattr(service.store, "commit", fail)
    with pytest.raises(GatewayError) as error:
        await service.self_test(service.config.registry.find("a"), "startup_voice", time.monotonic() + 10)
    assert error.value.code == "snapshot_commit_failed"
    assert service.state == "storage_error"
    assert service.recovery_attempts == 0
    assert not list(service.config.state_dir.glob("*.flac"))


async def test_stale_or_corrupt_snapshot_never_falls_back(service):
    atomic_write(service.config.snapshot_path, b"not: [valid")
    import yaml

    with pytest.raises(yaml.YAMLError):
        service.store.select()


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipe")
async def test_real_local_pipe_handshake_and_bounded_close(service):
    identity = {"pid": os.getpid(), "created": current_creation_time(), "nonce": uuid.uuid4().hex}
    identity["pipe"] = "\\\\.\\pipe\\GSVGateway-test-" + uuid.uuid4().hex
    startup = {"phase": "startup_self_test"}
    server = ControlServer(identity["pipe"], identity["nonce"], service, identity, startup=startup)
    await server.start()
    try:
        result = await asyncio.wait_for(asyncio.to_thread(control_request, identity, "status", 65536, 2), 4)
        assert result["nonce"] == identity["nonce"] and result["ready"]
        assert result["startup_phase"] == "startup_self_test"
        result = await asyncio.wait_for(asyncio.to_thread(control_request, identity, "shutdown", 65536, 2), 4)
        assert service.shutdown.is_set()
    finally:
        await asyncio.wait_for(server.close(), 3)


def test_stop_and_status_survive_invalid_configuration(config, monkeypatch, capsys):
    state = {
        "config_path": str(config.path),
        "pid": os.getpid(),
        "created": "test",
        "nonce": "test",
        "pipe": "local",
    }
    write_state(control_state_path(config.path), state)
    config.path.write_text("invalid edited JSON")
    monkeypatch.setattr("gsv_gateway.cli.process_matches", lambda *_: True)
    monkeypatch.setattr(
        "gsv_gateway.cli.control_request", lambda *_: {"ready": True, "state": "ready", **state}
    )
    assert main(["status", "--config", str(config.path), "--json"]) == 0
    assert '"ready": true' in capsys.readouterr().out


async def test_official_sdk_against_in_process_asgi(service):
    import httpx
    from openai import AsyncOpenAI
    from gsv_gateway.api import create_app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service))) as http:
        client = AsyncOpenAI(
            api_key=service.tokens[0].decode("ascii"), base_url="http://test/v1", http_client=http, max_retries=0
        )
        models = await client.models.list()
        assert [item.id for item in models.data] == ["gpt-sovits"]
        async with client.audio.speech.with_streaming_response.create(
            model="gpt-sovits", voice="a", input="test", stream_format="audio"
        ) as response:
            result = await response.read()
            assert len(result) > 100 and response.status_code == 200


@pytest.mark.parametrize("status", [400, 401, 404, 429, 503])
async def test_official_sdk_recognizes_error_contract(service, monkeypatch, status):
    import httpx
    from openai import APIStatusError, AsyncOpenAI
    from gsv_gateway.api import create_app

    if status == 429:

        def full(*_):
            raise GatewayError("queue_full", "full", 429)

        monkeypatch.setattr(service.scheduler, "submit", full)
    if status == 503:
        service.state = "recovering"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service))) as http:
        client = AsyncOpenAI(
            api_key="invalid" if status == 401 else service.tokens[0].decode("ascii"),
            base_url="http://test/v1",
            http_client=http,
            max_retries=0,
        )
        with pytest.raises(APIStatusError) as error:
            if status == 404:
                await client.models.retrieve("missing")
            else:
                await client.audio.speech.create(
                    model="gpt-sovits", voice="missing" if status == 400 else "a", input="test"
                )
        assert error.value.status_code == status
        assert error.value.response.headers["x-request-id"]
