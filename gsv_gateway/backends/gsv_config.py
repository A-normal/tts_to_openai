import wave
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from ..config import (
    Backend,
    ConfigConstraint,
    Engine,
    GatewayConfig,
    Model,
    VoiceRegistry,
    asset_stamp,
    fingerprint,
    resolve_path,
)

TYPE = "gpt_sovits"
HOST = "127.0.0.1"
PORT = 9880
ENDPOINT = f"http://{HOST}:{PORT}"


class Auto(Model):
    python_executable: StrictStr = ""
    arguments: list[StrictStr] = Field(default_factory=lambda: ["-I", "api_v2.py"])
    working_directory: StrictStr = ""
    config_template: StrictStr = ""
    runtime_config: StrictStr = "data/backend/tts-infer.yaml"


class GSVBackend(Backend):
    type: Literal["gpt_sovits"] = TYPE
    mode: Literal["connect", "auto"] = "auto"
    exclusive: Literal[True] = True
    auto: Auto = Field(default_factory=Auto)
    last_good_config: StrictStr = "data/backend/last-good.yaml"


class Inference(Model):
    streaming_mode: Literal[1] = 1
    text_split_method: StrictStr = "cut5"
    batch_size: Literal[1] = 1
    split_bucket: Literal[False] = False
    parallel_infer: Literal[False] = False
    use_cuda_graph: StrictBool = False


class GSVGatewayConfig(GatewayConfig):
    backend: GSVBackend = Field(default_factory=GSVBackend)
    inference: Inference = Field(default_factory=Inference)

    @model_validator(mode="after")
    def listener_collision(self):
        if self.server.port == PORT and self.server.host in {HOST, "0.0.0.0", "localhost", "::", "::ffff:" + HOST}:
            raise ConfigConstraint("server.port", f"网关监听地址与 GSV 的固定回环 {PORT} 端口冲突。")
        return self

    def backend_files(self) -> tuple[dict[str, str], tuple[str, ...]]:
        auto = self.backend.auto
        return {"working": auto.runtime_config}, (auto.config_template,) if auto.config_template else ()


class GSVEngine(Engine):
    id: Literal["gpt-sovits"] = "gpt-sovits"
    created: StrictInt = Field(default=1794009600, ge=0)


class GSVVoiceRegistry(VoiceRegistry):
    backend_type: Literal["gpt_sovits"] = TYPE
    engine: GSVEngine = Field(default_factory=GSVEngine)
    assets_root: StrictStr

    @model_validator(mode="after")
    def prompt_language_cache(self):
        prompts = {}
        for voice in self.voices:
            options = voice.backend_options
            text, lang = options.get("prompt_text"), options.get("prompt_lang")
            if text in prompts and prompts[text] != lang:
                raise ConfigConstraint("voices", "相同参考文本配置了不同的参考语言。")
            prompts[text] = lang
        return self


class GSVOptions(Model):
    gpt_weights: StrictStr
    sovits_weights: StrictStr
    ref_audio_path: StrictStr
    prompt_text: StrictStr = Field(min_length=1)
    prompt_lang: StrictStr
    text_lang: StrictStr
    expected_sample_rate: StrictInt = Field(gt=0, le=192000)


def validate_gsv_assets(config, voice) -> tuple[GSVOptions, dict, dict]:
    options = GSVOptions.model_validate(voice.backend_options)
    assets_root = resolve_path(config.voices_path.parent, config.registry.assets_root)
    paths = {
        k: resolve_path(assets_root, getattr(options, k))
        for k in ("gpt_weights", "sovits_weights", "ref_audio_path")
    }
    for path in paths.values():
        if not path.is_file():
            raise ValueError("Voice asset missing")
    stamps = {key: asset_stamp(path) for key, path in paths.items()}
    with wave.open(str(paths["ref_audio_path"]), "rb") as wav:
        duration = wav.getnframes() / wav.getframerate()
        if not 3 <= duration <= 10:
            raise ValueError("Reference audio must be 3–10 seconds")
    values = options.model_dump() | {k: str(v) for k, v in paths.items()}
    digests = {k: fingerprint(p) for k, p in paths.items()}
    if any(asset_stamp(path) != stamps[key] for key, path in paths.items()):
        raise ValueError("Voice asset changed during validation")
    return GSVOptions.model_validate(values), digests, stamps
