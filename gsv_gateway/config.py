import hashlib
import json
import os
import wave
from pathlib import Path
from typing import Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from . import jsonc


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_as_integer(cls, value):
        if isinstance(value, dict):
            for key, item in value.items():
                field = cls.model_fields.get(key)
                if field and get_origin(field.annotation) is Literal:
                    choices = get_args(field.annotation)
                    if isinstance(item, bool) and not any(
                        type(choice) is bool and item == choice for choice in choices
                    ):
                        raise ValueError("Boolean is not a numeric configuration constant")
                    if any(type(choice) is bool for choice in choices) and not isinstance(item, bool):
                        raise ValueError("Boolean configuration constant required")
        return value


Positive = Field(gt=0, allow_inf_nan=False)


class Server(Model):
    host: StrictStr = "127.0.0.1"
    port: StrictInt = Field(default=16681, ge=1, le=65535)
    max_request_bytes: StrictInt = Field(default=65536, ge=1024, le=65536)
    max_concurrent_requests: StrictInt = Field(default=32, ge=1, le=1024)
    body_timeout_seconds: float = Field(default=15, gt=0, allow_inf_nan=False)


class Auth(Model):
    token_file: StrictStr = "secrets/api-token.txt"


class Auto(Model):
    python_executable: StrictStr = ""
    arguments: list[StrictStr] = Field(default_factory=lambda: ["-I", "api_v2.py"])
    working_directory: StrictStr = ""
    config_template: StrictStr = ""
    runtime_config: StrictStr = "data/backend/tts-infer.yaml"


class Recovery(Model):
    max_attempts: StrictInt = Field(default=3, ge=0, le=3)
    backoff_seconds: list[float] = Field(default_factory=lambda: [2, 5, 15])

    @model_validator(mode="after")
    def bounds(self):
        import math

        if len(self.backoff_seconds) < self.max_attempts or any(
            not math.isfinite(x) or x < 0 for x in self.backoff_seconds
        ):
            raise ValueError("Recovery delays must cover every attempt")
        return self


class Backend(Model):
    type: Literal["gpt_sovits"] = "gpt_sovits"
    mode: Literal["connect", "auto"] = "auto"
    exclusive: Literal[True] = True
    auto: Auto = Field(default_factory=Auto)
    recovery: Recovery = Field(default_factory=Recovery)
    last_good_config: StrictStr = "data/backend/last-good.yaml"


class Queue(Model):
    max_pending: StrictInt = Field(default=8, ge=1, le=1024)
    wait_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)


class Timeouts(Model):
    startup_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    task_total_seconds: float = Field(default=600, gt=0, allow_inf_nan=False)
    model_operation_seconds: float = Field(default=180, gt=0, allow_inf_nan=False)
    first_audio_seconds: float = Field(default=180, gt=0, allow_inf_nan=False)
    first_encoded_audio_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    upstream_idle_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    disconnect_drain_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    downstream_stall_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    shutdown_drain_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)


class Audio(Model):
    ffmpeg_executable: StrictStr | None = None
    ffprobe_executable: StrictStr | None = None
    default_response_format: Literal["mp3"] = "mp3"
    pcm_sample_rate: Literal[24000] = 24000
    opus_sample_rate: Literal[48000] = 48000
    mp3_bitrate_kbps: StrictInt = Field(default=128, ge=32, le=320)
    opus_bitrate_kbps: StrictInt = Field(default=64, ge=16, le=256)
    aac_bitrate_kbps: StrictInt = Field(default=128, ge=32, le=320)
    max_buffer_bytes: StrictInt = Field(default=1048576, ge=16384, le=16777216)
    stderr_capture_bytes: StrictInt = Field(default=65536, ge=0, le=1048576)


class Inference(Model):
    streaming_mode: Literal[1] = 1
    text_split_method: StrictStr = "cut5"
    batch_size: Literal[1] = 1
    split_bucket: Literal[False] = False
    parallel_infer: Literal[False] = False
    use_cuda_graph: StrictBool = False


class Logging(Model):
    directory: StrictStr = "data/logs"
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    rotate_bytes: StrictInt = Field(default=10485760, ge=4096)
    backup_count: StrictInt = Field(default=5, ge=1, le=100)
    log_input: Literal[False] = False
    log_reference_text: Literal[False] = False


class StartupTest(Model):
    voice: StrictStr
    text: StrictStr = "你好，这是启动自检。正在检查语音生成和音频编码。你好，测试结束。"
    response_format: Literal["flac"] = "flac"
    speed: Literal[1] = 1


class Control(Model):
    max_message_bytes: StrictInt = Field(default=65536, ge=1024, le=65536)
    timeout_seconds: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)


class GatewayConfig(Model):
    schema_version: Literal[1] = 1
    server: Server = Field(default_factory=Server)
    auth: Auth = Field(default_factory=Auth)
    voices_file: StrictStr = "voices.jsonc"
    runtime_dir: StrictStr = "data"
    backend: Backend = Field(default_factory=Backend)
    queue: Queue = Field(default_factory=Queue)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    audio: Audio = Field(default_factory=Audio)
    inference: Inference = Field(default_factory=Inference)
    logging: Logging = Field(default_factory=Logging)
    startup_test: StartupTest
    control: Control = Field(default_factory=Control)

    @model_validator(mode="after")
    def collision(self):
        if self.server.port == 9880 and self.server.host in {
            "127.0.0.1",
            "0.0.0.0",
            "localhost",
            "::",
            "::ffff:127.0.0.1",
        }:
            raise ValueError("Gateway listener overlaps fixed local GSV endpoint")
        if not self.startup_test.text.strip() or len(self.startup_test.text) > 4096:
            raise ValueError("Invalid startup test text")
        return self


class Engine(Model):
    id: Literal["gpt-sovits"] = "gpt-sovits"
    created: StrictInt = Field(default=1794009600, ge=0)
    owned_by: StrictStr = "local"


class Voice(Model):
    id: StrictStr = Field(min_length=1, max_length=128)
    name: StrictStr
    aliases: list[StrictStr] = Field(default_factory=list)
    declared_languages: list[StrictStr] = Field(min_length=1)
    backend_options: dict

    def public(self):
        return self.model_dump(exclude={"backend_options"}) | {"streaming": "segmented"}


class VoiceRegistry(Model):
    schema_version: Literal[1] = 1
    backend_type: Literal["gpt_sovits"] = "gpt_sovits"
    engine: Engine = Field(default_factory=Engine)
    assets_root: StrictStr
    voices: list[Voice] = Field(min_length=1)

    @model_validator(mode="after")
    def identifiers(self):
        seen = set()
        prompts = {}
        for voice in self.voices:
            for alias in [voice.id, *voice.aliases]:
                if not alias.strip() or alias in seen:
                    raise ValueError("Voice ID/alias must be globally unique")
                seen.add(alias)
            options = voice.backend_options
            text, lang = options.get("prompt_text"), options.get("prompt_lang")
            if text in prompts and prompts[text] != lang:
                raise ValueError("GSV prompt language cache conflict")
            prompts[text] = lang
        return self

    def find(self, value: str) -> Voice:
        for voice in self.voices:
            if value == voice.id or value in voice.aliases:
                return voice
        raise KeyError(value)


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def fingerprint(path: Path) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("File changed while fingerprinting")
    return digest.hexdigest()


class LoadedConfig:
    def __init__(self, path: Path, overrides: dict | None = None, runtime_root: Path | None = None):
        self.path = path.resolve(strict=True)
        self.base = self.path.parent
        self.overrides = dict(overrides or {})
        raw = jsonc.load(self.path)
        for key, value in self.overrides.items():
            section, field = key.split(".", 1)
            raw.setdefault(section, {})[field] = value
        self.value = GatewayConfig.model_validate(raw)
        self.runtime_root = (runtime_root or Path(__file__).resolve().parent.parent / "runtime").resolve()
        self.voices_path = self.resolve(self.value.voices_file)
        self.registry = VoiceRegistry.model_validate(jsonc.load(self.voices_path))
        self.assets_root = resolve_path(self.voices_path.parent, self.registry.assets_root)
        self.registry_digest = fingerprint(self.voices_path)
        self.instance_id = instance_id(self.path)
        effective = self.value.model_dump() | {
            "_voices_digest": self.registry_digest,
            "_runtime_root": str(self.runtime_root),
        }
        self.effective_digest = hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()
        self.registry.find(self.value.startup_test.voice)
        if self.registry.find(self.value.startup_test.voice).id != self.value.startup_test.voice:
            raise ValueError("Startup voice must use its canonical ID")
        self.state_dir = self.resolve(self.value.runtime_dir)
        self.snapshot_path = self.resolve(self.value.backend.last_good_config)
        self.working_path = self.resolve(self.value.backend.auto.runtime_config)
        self.token_path = self.resolve(self.value.auth.token_file)
        protected = [self.path, self.voices_path, self.token_path]
        if self.value.backend.auto.config_template:
            protected.append(self.resolve(self.value.backend.auto.config_template))
        same_file = (
            self.snapshot_path.exists()
            and self.working_path.exists()
            and os.path.samefile(self.snapshot_path, self.working_path)
        )
        aliases_protected = any(
            p == target or p.exists() and target.exists() and os.path.samefile(p, target)
            for p in [self.snapshot_path, self.working_path]
            for target in protected
        )
        if self.snapshot_path == self.working_path or same_file or aliases_protected:
            raise ValueError("Private backend files must be separate from source/config/token files")
        for p in [self.snapshot_path, self.working_path, self.resolve(self.value.logging.directory)]:
            if not p.is_relative_to(self.state_dir):
                raise ValueError("Backend state and logs must be inside the private runtime_dir")

    def resolve(self, value: str) -> Path:
        return resolve_path(self.base, value)

    def read_token(self) -> str:
        from .security import require_private

        require_private(self.token_path)
        token = self.token_path.read_text(encoding="utf-8-sig").strip()
        if not token or any(c.isspace() for c in token) or len(token) < 43:
            raise ValueError("Token file must contain a single unpredictable token of at least 43 characters")
        return token

    def assert_registry_unchanged(self):
        if fingerprint(self.voices_path) != self.registry_digest:
            raise ValueError("Voice registry changed; restart required")


class GSVOptions(Model):
    gpt_weights: StrictStr
    sovits_weights: StrictStr
    ref_audio_path: StrictStr
    prompt_text: StrictStr = Field(min_length=1)
    prompt_lang: StrictStr
    text_lang: StrictStr
    expected_sample_rate: StrictInt = Field(gt=0, le=192000)


def validate_gsv_assets(config: LoadedConfig, voice: Voice) -> tuple[GSVOptions, dict, dict]:
    options = GSVOptions.model_validate(voice.backend_options)
    paths = {
        k: resolve_path(config.assets_root, getattr(options, k))
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


def asset_stamp(path):
    stat = Path(path).stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def instance_id(path):
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()[:32]


def control_state_path(path):
    path = Path(path).resolve()
    return path.parent / ".gsv-gateway" / (instance_id(path) + ".json")
