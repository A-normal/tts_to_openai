import json
import struct

import pytest
from pydantic import ValidationError

from gsv_gateway.backends.gsv_config import GSVGatewayConfig, GSVVoiceRegistry
from gsv_gateway.config import LoadedConfig
from gsv_gateway.jsonc import loads
from gsv_gateway.wav import WAVParser


def test_jsonc_preserves_strings_and_positions():
    assert loads(r"""{
      // URL and paths must survive
      "url":"http://127.0.0.1:9880/*not a comment*/",
      "path":"C:\\a\\b", /* explanatory text */ "quoted":"a\"//b"
    }""") == {"url": "http://127.0.0.1:9880/*not a comment*/", "path": "C:\\a\\b", "quoted": 'a"//b'}


@pytest.mark.parametrize("source", ['{"a":1,}', '{"a":1,"a":2}', '{"a":NaN}', "{/*broken"])
def test_jsonc_rejects_ambiguous_inputs(source):
    with pytest.raises(ValueError):
        loads(source)


def test_mode_defaults_and_removed_fields():
    config = GSVGatewayConfig.model_validate({"startup_test": {"voice": "a"}})
    assert config.timeouts.first_audio_seconds == 180
    assert config.timeouts.first_encoded_audio_seconds == 30
    assert config.server.max_concurrent_requests == 32
    for removed in ({"connect_url": "http://remote"}, {"auto": {"port": 16680}}):
        with pytest.raises(ValidationError):
            GSVGatewayConfig.model_validate({"startup_test": {"voice": "a"}, "backend": removed})


@pytest.mark.parametrize("field,value", [("exclusive", 1), ("exclusive", "true")])
def test_boolean_config_constants_are_strict(field, value):
    with pytest.raises(ValidationError):
        GSVGatewayConfig.model_validate({"startup_test": {"voice": "a"}, "backend": {field: value}})


@pytest.mark.parametrize(
    "changes",
    [
        {"server": {"port": 9880}},
        {"server": {"port": True}},
        {"timeouts": {"first_audio_seconds": float("inf")}},
        {"startup_test": {"voice": "a", "text": "  "}},
    ],
)
def test_config_rejects_invalid_contract(changes):
    with pytest.raises(ValidationError):
        GSVGatewayConfig.model_validate({"startup_test": {"voice": "a"}} | changes)


def test_relative_paths_ignore_cwd(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)
    again = LoadedConfig(config.path)
    assert again.state_dir == tmp_path / "data"
    assert again.registry_digest == config.registry_digest


def test_snapshot_work_file_cannot_alias(config):
    data = json.loads(config.path.read_text())
    data["backend"]["last_good_config"] = "data/backend/tts-infer.yaml"
    config.path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        LoadedConfig(config.path)


def test_alias_conflicts(config):
    data = config.registry.model_dump()
    data["voices"][1]["aliases"] = ["a"]
    with pytest.raises(ValidationError):
        GSVVoiceRegistry.model_validate(data)


def wav_header(rate=48000):
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    return (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFFFF)
        + b"WAVEJUNK"
        + struct.pack("<I", 3)
        + b"abc\0fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + b"\0" * 4
    )


@pytest.mark.parametrize("width", [1, 2, 7, 43, 65536])
def test_wav_fragmentation_optional_chunks_and_unknown_length(width):
    source = wav_header() + b"\1\0\2\0\3\0"
    parser = WAVParser(48000)
    pcm = b"".join(parser.feed(source[i : i + width]) for i in range(0, len(source), width))
    parser.finish()
    assert pcm == b"\1\0\2\0\3\0"
    assert parser.samples == 3


def test_wav_partial_sample_and_wrong_rate_rejected():
    parser = WAVParser(48000)
    parser.feed(wav_header() + b"\1\0\2")
    with pytest.raises(ValueError):
        parser.finish()
    with pytest.raises(ValueError):
        WAVParser(24000).feed(wav_header())
