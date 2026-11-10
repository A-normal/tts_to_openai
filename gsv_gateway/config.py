import hashlib
import json
import os
from itertools import combinations
from pathlib import Path
from typing import Literal, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from . import jsonc


class ConfigConstraint(ValueError):
    """A configuration diagnostic safe to show without echoing input values."""

    def __init__(self, field: str, reason: str):
        super().__init__(reason)
        self.field = field


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
    token_file: StrictStr = "secrets/api-tokens.jsonc"


class TokenEntry(Model):
    id: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    token: StrictStr = Field(min_length=43, max_length=512, pattern=r"^[A-Za-z0-9._~+/\-]+=*$")
    enabled: StrictBool


class TokenFile(Model):
    tokens: list[TokenEntry] = Field(min_length=1, max_length=128)


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
    type: StrictStr
    mode: Literal["connect"] = "connect"
    recovery: Recovery = Field(default_factory=Recovery)
    last_good_config: StrictStr | None = None


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
    backend: Backend
    queue: Queue = Field(default_factory=Queue)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    audio: Audio = Field(default_factory=Audio)
    logging: Logging = Field(default_factory=Logging)
    startup_test: StartupTest
    control: Control = Field(default_factory=Control)

    @model_validator(mode="after")
    def valid_startup_text(self):
        if not self.startup_test.text.strip() or len(self.startup_test.text) > 4096:
            raise ValueError("Invalid startup test text")
        return self

    def backend_files(self) -> tuple[dict[str, str], tuple[str, ...]]:
        """Additional writable state and read-only source paths, respectively."""
        return {}, ()


class Engine(Model):
    id: StrictStr = Field(min_length=1, max_length=128)
    created: StrictInt = Field(default=0, ge=0)
    owned_by: StrictStr = "local"


class Voice(Model):
    id: StrictStr = Field(min_length=1, max_length=128)
    name: StrictStr
    aliases: list[StrictStr] = Field(default_factory=list)
    declared_languages: list[StrictStr] = Field(min_length=1)
    backend_options: dict

    def public(self, streaming: str):
        return self.model_dump(exclude={"backend_options"}) | {"streaming": streaming}


class VoiceRegistry(Model):
    schema_version: Literal[1] = 1
    backend_type: StrictStr
    engine: Engine
    voices: list[Voice] = Field(min_length=1)

    @model_validator(mode="after")
    def identifiers(self):
        seen = set()
        for voice in self.voices:
            for alias in [voice.id, *voice.aliases]:
                if not alias.strip() or alias in seen:
                    raise ValueError("Voice ID/alias must be globally unique")
                seen.add(alias)
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


def load_gateway_config(path, overrides=None):
    from .backends import DEFAULT_BACKEND, get_registration

    raw = jsonc.load(path)
    if not isinstance(raw, dict):
        raise ValueError("Gateway configuration must be an object")
    for key, value in (overrides or {}).items():
        section, field = key.split(".", 1)
        raw.setdefault(section, {})[field] = value
    backend = raw.get("backend", {})
    name = backend.get("type", DEFAULT_BACKEND) if isinstance(backend, dict) else DEFAULT_BACKEND
    registration = get_registration(name)
    return registration.gateway_config.model_validate(raw), registration


def load_voice_registry(path, registration, backend_type):
    registry = registration.voice_registry.model_validate(jsonc.load(path))
    if registry.backend_type != backend_type:
        raise ValueError("Voice registry backend_type must match backend.type")
    return registry


def same_path(first, second):
    return first == second or first.exists() and second.exists() and os.path.samefile(first, second)


class LoadedConfig:
    def __init__(self, path: Path, overrides: dict | None = None, runtime_root: Path | None = None):
        self.path = path.resolve(strict=True)
        self.base = self.path.parent
        self.overrides = dict(overrides or {})
        self.value, self.backend_registration = load_gateway_config(self.path, self.overrides)
        self.runtime_root = (runtime_root or Path(__file__).resolve().parent.parent / "runtime").resolve()
        self.voices_path = self.resolve(self.value.voices_file)
        self.registry = load_voice_registry(self.voices_path, self.backend_registration, self.value.backend.type)
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
        snapshot = self.value.backend.last_good_config
        self.snapshot_path = (
            self.resolve(snapshot)
            if snapshot is not None
            else self.state_dir / "backend" / f"{self.value.backend.type}-last-good.yaml"
        ).resolve()
        self.token_path = self.resolve(self.value.auth.token_file)
        writable, sources = self.value.backend_files()
        self.backend_paths = {name: self.resolve(path) for name, path in writable.items()}
        private_paths = [self.snapshot_path, *self.backend_paths.values()]
        protected = [self.path, self.voices_path, self.token_path, *(self.resolve(path) for path in sources)]
        if any(same_path(a, b) for a, b in combinations(private_paths, 2)) or any(
            same_path(path, target) for path in private_paths for target in protected
        ):
            raise ValueError("Private backend files must be separate from source/config/token files")
        for p in [*private_paths, self.resolve(self.value.logging.directory)]:
            if not p.is_relative_to(self.state_dir):
                raise ValueError("Backend state and logs must be inside the private runtime_dir")

    def resolve(self, value: str) -> Path:
        return resolve_path(self.base, value)

    def public_voices(self) -> list[dict]:
        return [voice.public(self.backend_registration.streaming) for voice in self.registry.voices]

    def read_tokens(self) -> tuple[bytes, ...]:
        from .security import require_private

        if self.token_path.suffix.lower() != ".jsonc":
            raise ValueError("auth.token_file must point to a private .jsonc token file")
        try:
            require_private(self.token_path)
        except Exception:
            raise ValueError("Token file missing or permissions could not be verified") from None
        try:
            raw = jsonc.load(self.token_path)
        except (OSError, ValueError, RecursionError):
            raise ValueError("Token file is unreadable or contains invalid JSONC") from None
        try:
            entries = TokenFile.model_validate(raw).tokens
        except ValidationError as exc:
            location = exc.errors(include_input=False)[0]["loc"]
            if len(location) >= 2 and location[0] == "tokens" and isinstance(location[1], int):
                raise ValueError(f"Invalid token entry #{location[1] + 1}: check id, token and enabled") from None
            raise ValueError("Token file must contain a tokens array with 1 to 128 entries") from None

        ids, values, active = set(), set(), []
        for number, entry in enumerate(entries, 1):
            if entry.id in ids or entry.token in values:
                raise ValueError(f"Duplicate token ID or value at entry #{number}")
            ids.add(entry.id)
            values.add(entry.token)
            if entry.enabled:
                active.append(entry.token.encode("ascii"))
        if not active:
            raise ValueError("At least one token must be enabled")
        return tuple(active)

    def assert_registry_unchanged(self):
        if fingerprint(self.voices_path) != self.registry_digest:
            raise ValueError("Voice registry changed; restart required")


def asset_stamp(path):
    stat = Path(path).stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def instance_id(path):
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()[:32]


def control_state_path(path):
    path = Path(path).resolve()
    return path.parent / ".gsv-gateway" / (instance_id(path) + ".json")
