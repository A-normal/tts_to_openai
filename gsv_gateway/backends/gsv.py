import copy
import socket
import time

import httpx
import yaml

from ..config import asset_stamp, validate_gsv_assets
from ..errors import GatewayError
from ..wav import WAVParser
from ..windows import LaunchSpec
from .base import AudioSession


ENDPOINT = "http://127.0.0.1:9880"


class GSVAdapter:
    def __init__(self, config, transport=None):
        self.config = config
        self.state = "unknown_clean"
        self.loaded_voice = None
        self.assets = {}
        self.asset_stamps = {}
        self.client = httpx.AsyncClient(
            base_url=ENDPOINT,
            trust_env=False,
            transport=transport,
            timeout=httpx.Timeout(180, connect=5, pool=5),
        )

    def describe(self):
        return self.config.registry.engine.model_dump() | {"object": "model"}

    def validate(self):
        for voice in self.config.registry.voices:
            options, digests, stamps = validate_gsv_assets(self.config, voice)
            self.assets[voice.id] = options, digests
            self.asset_stamps[voice.id] = stamps

    @staticmethod
    def _stamp(path):
        return asset_stamp(path)

    def assert_voice(self, voice):
        try:
            self.config.assert_registry_unchanged()
        except (ValueError, OSError):
            raise GatewayError("config_changed", "声音配置已变更，请重启网关。", 503) from None
        options, digests = self.assets[voice.id]
        for key in digests:
            try:
                unchanged = self._stamp(getattr(options, key)) == self.asset_stamps[voice.id][key]
            except OSError:
                unchanged = False
            if not unchanged:
                raise GatewayError("config_changed", "声音资产已变更，请重启网关。", 503)
        return options

    async def probe(self):
        try:
            response = await self.client.get("/openapi.json", timeout=5)
            if response.status_code != 200:
                return "incompatible"
            paths = response.json().get("paths", {})
            required = {
                "/tts": "post",
                "/set_gpt_weights": "get",
                "/set_sovits_weights": "get",
                "/set_refer_audio": "get",
            }
            return (
                "ready"
                if all(method in paths.get(path, {}) for path, method in required.items())
                else "incompatible"
            )
        except httpx.ConnectError as error:
            # Only an actual connection refusal authorizes auto launch. DNS,
            # inaccessible networking and other failures do not imply absence.
            cause = error
            while cause:
                if (
                    isinstance(cause, ConnectionRefusedError)
                    or isinstance(cause, OSError)
                    and getattr(cause, "winerror", None) == 10061
                ):
                    return "unreachable"
                cause = cause.__cause__ or cause.__context__
            with socket.socket() as sock:
                sock.settimeout(1)
                return "unreachable" if sock.connect_ex(("127.0.0.1", 9880)) in (10061, 111) else "uncertain"
        except (httpx.HTTPError, ValueError):
            return "uncertain"

    async def prepare(self, voice, operation):
        options = self.assert_voice(voice)
        if self.state == "loaded" and self.loaded_voice == voice.id:
            return
        self.state, self.loaded_voice = "loading", None
        try:
            for path, params in [
                ("/set_gpt_weights", {"weights_path": options.gpt_weights}),
                ("/set_sovits_weights", {"weights_path": options.sovits_weights}),
                ("/set_refer_audio", {"refer_audio_path": options.ref_audio_path}),
            ]:
                response = await operation.wait(
                    self.client.get(path, params=params), self.config.value.timeouts.model_operation_seconds
                )
                if response.status_code != 200 or response.json().get("message") != "success":
                    raise ValueError("Setter rejected")
            self.state, self.loaded_voice = "loaded", voice.id
        except BaseException as error:
            self.state = "uncertain"
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise GatewayError(
                "backend_prepare_failed",
                "后端声音准备失败，状态需要恢复。",
                504 if isinstance(error, TimeoutError) else 502,
                uncertain=True,
            ) from None

    async def synthesize(self, voice, text, speed, operation):
        options = self.assert_voice(voice)
        payload = self.config.value.inference.model_dump() | {
            "text": text,
            "text_lang": options.text_lang,
            "ref_audio_path": options.ref_audio_path,
            "prompt_text": options.prompt_text,
            "prompt_lang": options.prompt_lang,
            "speed_factor": speed,
            "media_type": "wav",
        }
        first_deadline = time.monotonic() + self.config.value.timeouts.first_audio_seconds
        response = None
        try:
            response = await operation.wait(
                self.client.send(self.client.build_request("POST", "/tts", json=payload), stream=True),
                self.config.value.timeouts.first_audio_seconds,
            )
            if response.status_code != 200:
                raise ValueError("Synthesis rejected")
            parser = WAVParser(options.expected_sample_rate)
            chunks = response.aiter_bytes().__aiter__()
            first = b""
            while not first:
                block = await operation.wait(anext(chunks), max(0.001, first_deadline - time.monotonic()))
                first = parser.feed(block)
            session = AudioSession(parser.sample_rate, None, first_pcm_monotonic=time.monotonic())

            async def pcm():
                try:
                    session.samples += len(first) // 2
                    session.non_silent |= any(first)
                    yield first
                    while True:
                        try:
                            block = await operation.wait(
                                anext(chunks), self.config.value.timeouts.upstream_idle_seconds
                            )
                        except StopAsyncIteration:
                            break
                        block = parser.feed(block)
                        if block:
                            session.samples += len(block) // 2
                            session.non_silent |= any(block)
                            yield block
                    parser.finish()
                    session.complete = True
                except BaseException:
                    self.state, self.loaded_voice = "uncertain", None
                    raise GatewayError(
                        "backend_stream_failed", "后端音频流未完整结束。", uncertain=True
                    ) from None
                finally:
                    await response.aclose()

            session.pcm = pcm()
            return session
        except BaseException as error:
            if response is not None:
                await response.aclose()
            self.state, self.loaded_voice = "uncertain", None
            raise GatewayError(
                "backend_first_audio_failed",
                "未能在期限内取得合法后端音频。",
                504 if isinstance(error, (TimeoutError, httpx.TimeoutException)) else 502,
                uncertain=True,
            ) from None

    def snapshot(self, voice):
        options, digests = self.assets[voice.id]
        return {
            "schema_version": 1,
            "backend_type": "gpt_sovits",
            "voice": voice.id,
            "registry_sha256": self.config.registry_digest,
            "asset_sha256": digests,
            "backend_options": options.model_dump(),
            "backend_payload": {"options": options.model_dump()},
        }

    def stage_working_config(self, voice):
        options = self.assert_voice(voice)
        template = self.config.resolve(self.config.value.backend.auto.config_template)
        # Read the trusted original template, never the possibly polluted work file.
        data = yaml.safe_load(template.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("custom"), dict):
            raise ValueError("GSV template requires a complete custom section")
        data = copy.deepcopy(data)
        data["custom"].update(t2s_weights_path=options.gpt_weights, vits_weights_path=options.sovits_weights)
        for key in ("device", "is_half", "bert_base_path", "cnhuhbert_base_path"):
            if key not in data["custom"]:
                raise ValueError("GSV template missing base configuration")
        from ..storage import atomic_write

        atomic_write(self.config.working_path, yaml.safe_dump(data, allow_unicode=True).encode())

    def build_launch_spec(self, voice):
        auto = self.config.value.backend.auto
        executable = self.config.resolve(auto.python_executable)
        cwd = self.config.resolve(auto.working_directory)
        args = list(auto.arguments)
        # Only the supported api script is accepted; reserved arguments have a
        # single owner and can never be overridden by a request or profile.
        if args.count("-I") > 1:
            raise ValueError("Duplicate isolated flag")
        args = [arg for arg in args if arg != "-I"]
        if len(args) != 1 or self.config.resolve(str(cwd / args[0])).name != "api_v2.py":
            raise ValueError("GSV arguments must contain only api_v2.py and optional -I")
        script = (cwd / args[0]).resolve()
        if not executable.is_file() or not script.is_file():
            raise ValueError("GSV executable/script missing")
        self.validate_default_port(script)
        self.stage_working_config(voice)
        return LaunchSpec(
            str(executable),
            ["-I", str(script), "-a", "127.0.0.1", "-c", str(self.config.working_path)],
            str(cwd),
        )

    @staticmethod
    def validate_default_port(script):
        import ast

        tree = ast.parse(script.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
            ):
                flags = [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
                if "-p" in flags or "--port" in flags:
                    default = next(
                        (keyword.value for keyword in node.keywords if keyword.arg == "default"), None
                    )
                    if isinstance(default, ast.Constant) and int(default.value) == 9880:
                        return
        raise ValueError("GSV API default port changed or cannot be verified; revalidate backend version")

    async def close(self):
        await self.client.aclose()
