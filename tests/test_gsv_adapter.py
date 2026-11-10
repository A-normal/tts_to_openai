import io
import json
import time
import wave

import httpx
import pytest

from tests.conftest import PCM
from gsv_gateway.backends.base import Operation
from gsv_gateway.backends.gsv import GSVAdapter
from gsv_gateway.config import LoadedConfig
from gsv_gateway.errors import GatewayError


def assets(config):
    root = config.base
    (root / "gpt.ckpt").write_bytes(b"fake test weight")
    (root / "sovits.pth").write_bytes(b"04fake test weight")
    with wave.open(str(root / "ref.wav"), "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(PCM * 6)
    data = config.registry.model_dump()
    for voice in data["voices"]:
        voice["backend_options"] = {
            "gpt_weights": "gpt.ckpt",
            "sovits_weights": "sovits.pth",
            "ref_audio_path": "ref.wav",
            "prompt_text": "test reference",
            "prompt_lang": "zh",
            "text_lang": "zh",
            "expected_sample_rate": 48000,
        }
    config.voices_path.write_text(json.dumps(data))
    return LoadedConfig(config.path)


async def test_gsv_prepare_mapping_and_cache(config):
    config = assets(config)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"message": "success"})

    adapter = GSVAdapter(config, httpx.MockTransport(handle))
    try:
        adapter.validate()
        voice = config.registry.find("a")
        await adapter.prepare(voice, Operation(time.monotonic() + 5))
        await adapter.prepare(voice, Operation(time.monotonic() + 5))
        assert [r.url.path for r in requests] == [
            "/set_gpt_weights",
            "/set_sovits_weights",
            "/set_refer_audio",
        ]
        assert all("authorization" not in r.headers for r in requests)
        assert adapter.client.trust_env is False
        await adapter.prepare(config.registry.find("b"), Operation(time.monotonic() + 5))
        assert requests[-1].url.path == "/set_refer_audio"
        assert len(requests) == 6
    finally:
        await adapter.close()


async def test_setter_partial_failure_is_uncertain(config):
    config = assets(config)
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(500 if len(calls) == 2 else 200, json={"message": "success"})

    adapter = GSVAdapter(config, httpx.MockTransport(handle))
    try:
        adapter.validate()
        with pytest.raises(GatewayError) as error:
            await adapter.prepare(config.registry.find("a"), Operation(time.monotonic() + 5))
        assert error.value.uncertain and adapter.state == "uncertain"
        assert calls == ["/set_gpt_weights", "/set_sovits_weights"]
    finally:
        await adapter.close()


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, data, fail=False):
        self.data, self.fail = data, fail

    async def __aiter__(self):
        for offset in range(0, len(self.data), 73):
            yield self.data[offset : offset + 73]
        if self.fail:
            raise httpx.ReadError("private upstream error")


@pytest.mark.parametrize("fail", [False, True])
async def test_pcm_normalization_and_complete_evidence(config, fail):
    config = assets(config)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(PCM)

    def handle(request):
        payload = json.loads(request.content)
        assert payload["streaming_mode"] == 1 and payload["parallel_infer"] is False
        return httpx.Response(200, stream=ChunkStream(output.getvalue(), fail))

    adapter = GSVAdapter(config, httpx.MockTransport(handle))
    try:
        adapter.validate()
        session = await adapter.synthesize(
            config.registry.find("a"), "test", 1, Operation(time.monotonic() + 5)
        )
        result = bytearray()
        try:
            async for block in session.pcm:
                result.extend(block)
        except GatewayError:
            assert fail and not session.complete and adapter.state == "uncertain"
        else:
            assert not fail and session.complete
        assert result == PCM
    finally:
        await adapter.close()


def test_default_port_is_statically_checked(tmp_path):
    script = tmp_path / "api_v2.py"
    script.write_text("parser.add_argument('-p','--port', default='9880')")
    GSVAdapter.validate_default_port(script)
    script.write_text("parser.add_argument('-p','--port', default='16680')")
    with pytest.raises(ValueError):
        GSVAdapter.validate_default_port(script)


async def test_gsv_owned_launch_validation_and_working_file(config):
    config = assets(config)
    raw = json.loads(config.path.read_text(encoding="utf-8"))
    raw["backend"].update(
        mode="auto",
        auto={"python_executable": "fake-python.exe", "working_directory": ".", "config_template": "source.yaml"},
    )
    config.path.write_text(json.dumps(raw), encoding="utf-8")
    (config.base / "fake-python.exe").touch()
    (config.base / "api_v2.py").write_text("parser.add_argument('--port', default=9880)", encoding="utf-8")
    template = config.base / "source.yaml"
    contents = "custom: {device: cpu, is_half: false, bert_base_path: bert, cnhuhbert_base_path: hubert}"
    template.write_text(contents, encoding="utf-8")
    config = LoadedConfig(config.path)
    adapter = GSVAdapter(config)
    try:
        adapter.validate()
        adapter.validate_launch()
        working = config.backend_paths["working"]
        assert not working.exists()
        voice = config.registry.find("a")
        spec = adapter.build_launch_spec(voice)
        assert spec.arguments == ["-I", str(config.base / "api_v2.py"), "-a", "127.0.0.1", "-c", str(working)]
        assert working.is_file() and template.read_text(encoding="utf-8") == contents
        assert adapter.snapshot(voice)["backend_type"] == "gpt_sovits"
        assert not config.snapshot_path.exists()
    finally:
        await adapter.close()


async def test_asset_or_registry_edit_requires_restart_without_backend_calls(config):
    config = assets(config)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"message": "success"})

    adapter = GSVAdapter(config, httpx.MockTransport(handle))
    try:
        adapter.validate()
        (config.base / "gpt.ckpt").write_bytes(b"changed")
        with pytest.raises(GatewayError) as error:
            await adapter.prepare(config.registry.find("a"), Operation(time.monotonic() + 5))
        assert error.value.code == "config_changed" and error.value.status == 503
        assert not calls
    finally:
        await adapter.close()
