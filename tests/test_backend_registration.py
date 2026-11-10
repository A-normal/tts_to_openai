import json
from typing import Literal

import httpx
import pytest
from pydantic import StrictInt

from gsv_gateway import audio, cli, environment, service as service_module
from gsv_gateway.api import create_app
from gsv_gateway.backends import BACKENDS, BackendRegistration
from gsv_gateway.config import Backend, GatewayConfig, LoadedConfig, Model, VoiceRegistry
from gsv_gateway.doctor import diagnose, load_diagnostic_config
from tests.conftest import FakeAdapter, ForegroundProcess, foreground_pipeline


class FixtureBackend(Backend):
    type: Literal["fixture_tts"]


class FixtureConfig(GatewayConfig):
    backend: FixtureBackend


class SpeakerOptions(Model):
    speaker: StrictInt


class FixtureAdapter(FakeAdapter):
    endpoint_label = "Fixture TTS endpoint"

    def validate(self):
        for voice in self.config.registry.voices:
            SpeakerOptions.model_validate(voice.backend_options)


@pytest.fixture
def fixture_backend(config, monkeypatch):
    monkeypatch.setitem(
        BACKENDS, "fixture_tts", BackendRegistration(FixtureConfig, VoiceRegistry, FixtureAdapter, "buffered")
    )
    gateway = json.loads(config.path.read_text(encoding="utf-8"))
    gateway["backend"] = {"type": "fixture_tts"}
    gateway["server"] = {"port": 9880}
    config.path.write_text(json.dumps(gateway), encoding="utf-8")
    voices = json.loads(config.voices_path.read_text(encoding="utf-8"))
    voices.pop("assets_root")
    voices["backend_type"] = "fixture_tts"
    voices["engine"] = {"id": "fixture-tts"}
    for voice in voices["voices"]:
        voice["backend_options"] = {"speaker": 1}
    config.voices_path.write_text(json.dumps(voices), encoding="utf-8")
    return config


async def test_registered_backend_through_startup_and_http(fixture_backend, monkeypatch, capsys):
    original = fixture_backend
    tools = audio.resolve_tools(original)
    monkeypatch.setattr(environment, "validate_environment", lambda _: (*tools, []))
    monkeypatch.setattr(service_module, "AudioPipeline", foreground_pipeline)
    monkeypatch.setattr(audio, "ManagedProcess", ForegroundProcess)

    config = LoadedConfig(original.path, runtime_root=original.base / "tools")
    service = service_module.GatewayService(config)
    assert isinstance(service.adapter, FixtureAdapter)
    assert not config.backend_paths and config.snapshot_path.name == "fixture_tts-last-good.yaml"
    service.scheduler.pipeline_factory = foreground_pipeline
    try:
        await service.start()
        assert service.state == "ready" and not service.owned
        assert service.store.select()[1] == "last_good"
        assert service.store.current["backend_type"] == "fixture_tts"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://test",
        ) as client:
            assert (await client.get("/v1/models")).status_code == 401
            client.headers["Authorization"] = "Bearer " + service.tokens[0].decode("ascii")
            assert (await client.get("/v1/models")).json()["data"][0]["id"] == "fixture-tts"
            public_voices = (await client.get("/voices")).json()["voices"]
            assert all(voice["streaming"] == "buffered" and "backend_options" not in voice for voice in public_voices)
            speech = {"model": "fixture-tts", "voice": "a", "input": "test", "response_format": "flac"}
            assert (await client.post("/v1/audio/speech", json=speech | {"model": "gpt-sovits"})).status_code == 404
            response = await client.post("/v1/audio/speech", json=speech)
            assert response.status_code == 200 and response.content.startswith(b"fLaC")
    finally:
        await service.close()

    report = await diagnose(config.path, full=False)
    checks = {row["code"]: row for row in report["checks"]}
    assert checks["VOICE_ASSETS"]["status"] == "OK"
    assert checks["BACKEND_ENDPOINT"]["message"] == "Fixture TTS endpoint: ready"
    assert "AUTO_LAUNCH_CONFIG" not in checks
    assert cli.main(["voices", "--config", str(config.path)]) == 0
    assert json.loads(capsys.readouterr().out)["voices"] == public_voices


@pytest.mark.parametrize(
    "gateway_type,voice_type,field",
    [("unregistered", "fixture_tts", "backend.type"), ("fixture_tts", "gpt_sovits", "backend_type")],
)
def test_backend_identity_must_be_registered_and_match(fixture_backend, gateway_type, voice_type, field):
    config = fixture_backend
    gateway = json.loads(config.path.read_text(encoding="utf-8"))
    gateway["backend"]["type"] = gateway_type
    config.path.write_text(json.dumps(gateway), encoding="utf-8")
    voices = json.loads(config.voices_path.read_text(encoding="utf-8"))
    voices["backend_type"] = voice_type
    config.voices_path.write_text(json.dumps(voices), encoding="utf-8")
    loaded, details = load_diagnostic_config(config.path)
    assert loaded is None and details[0]["field"] == field
