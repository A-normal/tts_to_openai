import json
import platform
import sys
from pathlib import Path

from pydantic import ValidationError

from . import jsonc
from .audio import select_audio_tools
from .backends import create_adapter
from .config import GatewayConfig, LoadedConfig, VoiceRegistry, resolve_path
from .python_probe import supported_python, verify_dependencies


_CONFIG_REASONS = {
    "Voice ID/alias must be globally unique": ("voices", "声音 ID/别名重复，或别名与另一声音 ID 冲突。"),
    "GSV prompt language cache conflict": ("voices", "相同参考文本配置了不同的参考语言。"),
    "Gateway listener overlaps fixed local GSV endpoint": (
        "server.port",
        "网关监听地址与 GSV 的固定回环 9880 端口冲突。",
    ),
    "Invalid startup test text": ("startup_test.text", "自检文本必须非空且不超过 4096 个 Unicode 码点。"),
    "Recovery delays must cover every attempt": (
        "backend.recovery",
        "恢复退避值必须非负且覆盖全部恢复次数。",
    ),
    "Startup voice must use its canonical ID": (
        "startup_test.voice",
        "启动声音必须使用规范 ID，不能使用别名。",
    ),
    "Private backend files must be separate from source/config/token files": (
        "backend",
        "工作 YAML 和成功快照必须彼此分离，且不能指向原配置或 Token 文件。",
    ),
    "Backend state and logs must be inside the private runtime_dir": (
        "runtime_dir",
        "日志、工作 YAML 和成功快照必须位于 runtime_dir 内。",
    ),
    "Boolean is not a numeric configuration constant": (None, "数字配置项不能填写 true/false。"),
    "Boolean configuration constant required": (
        None,
        "布尔配置项必须填写 true/false，不能使用数字或字符串。",
    ),
}


def _config_details(error, path, document):
    """Local diagnostics show location and rules, never input values or tracebacks."""
    base = {"file": str(Path(path).resolve()), "document": document}
    if isinstance(error, ValidationError):
        details = []
        for item in error.errors(include_input=False, include_url=False, include_context=False)[:20]:
            field = ".".join(str(part) for part in item["loc"]) or None
            kind = item["type"]
            reason = {
                "missing": "缺少必需字段。",
                "extra_forbidden": "不支持此配置项；请检查拼写或已删除的旧字段。",
                "literal_error": "必须使用 schema 中规定的固定值。",
                "string_type": "必须填写字符串。",
                "int_type": "必须填写整数，不能使用字符串或布尔值。",
                "float_type": "必须填写数字。",
                "bool_type": "必须填写 true/false。",
                "finite_number": "必须填写有限数值。",
                "greater_than": "数值必须大于 schema 规定的下限。",
                "greater_than_equal": "数值低于允许的最小值。",
                "less_than_equal": "数值超过允许的最大值。",
            }.get(kind, "字段类型、取值或字段间约束不符合配置 schema。")
            for known, (location, explanation) in _CONFIG_REASONS.items():
                if item["msg"] == "Value error, " + known:
                    field, reason = location or field, explanation
                    break
            details.append(base | {"field": field, "error_type": kind, "reason": reason})
        return details
    if isinstance(error, json.JSONDecodeError):
        return [
            base
            | {
                "error_type": "invalid_jsonc",
                "line": error.lineno,
                "column": error.colno,
                "reason": "JSONC 语法错误：检查引号、逗号、括号；不支持尾随逗号。",
            }
        ]
    if isinstance(error, FileNotFoundError):
        return [
            base
            | {
                "error_type": "file_missing",
                "reason": "文件不存在；相对 voices_file 以主配置文件所在目录解析。",
            }
        ]
    if isinstance(error, PermissionError):
        return [base | {"error_type": "permission_denied", "reason": "当前运行账户没有读取该文件的权限。"}]
    if isinstance(error, UnicodeError):
        return [base | {"error_type": "invalid_encoding", "reason": "配置文件必须使用 UTF-8 编码。"}]
    if isinstance(error, KeyError):
        return [
            base
            | {
                "field": "startup_test.voice",
                "error_type": "voice_not_found",
                "reason": "启动声音不在声音表中；请填写已注册的规范 ID。",
            }
        ]
    for known, (field, reason) in _CONFIG_REASONS.items():
        if isinstance(error, ValueError) and str(error) == known:
            return [base | {"field": field, "error_type": "config_constraint", "reason": reason}]
    reasons = {
        "Duplicate configuration key": "JSONC 中存在重复字段。",
        "Non-finite JSON number": "JSONC 不允许 NaN/Infinity。",
        "Configuration exceeds 4 MiB": "配置文件超过 4 MiB 上限。",
    }
    if isinstance(error, ValueError):
        reason = reasons.get(str(error), "配置路径或文件内容无效。")
        if str(error).startswith("Unterminated JSONC comment at offset "):
            reason = "块注释没有以 */ 结束。"
        return [base | {"error_type": "invalid_configuration", "reason": reason}]
    return [
        base
        | {"error_type": "config_read_failed", "reason": "配置读取或校验失败；请检查文件和目录访问权限。"}
    ]


def load_diagnostic_config(path, overrides=None, runtime_root=None):
    """Identify the owning document before validating the complete contract."""
    selected = Path(path).resolve()
    document = "gateway"
    try:
        raw = jsonc.load(selected)
        for key, value in (overrides or {}).items():
            section, field = key.split(".", 1)
            if isinstance(raw, dict):
                raw.setdefault(section, {})[field] = value
        gateway = GatewayConfig.model_validate(raw)
        selected = resolve_path(selected.parent, gateway.voices_file)
        document = "voices"
        VoiceRegistry.model_validate(jsonc.load(selected))
        selected, document = Path(path).resolve(), "gateway"
        config = LoadedConfig(Path(path), overrides, runtime_root)
        return config, []
    except Exception as error:
        return None, _config_details(error, selected, document)


async def diagnose(path, overrides=None, runtime_root=None, full=True):
    rows = []

    def row(code, status, message, **extra):
        rows.append({"code": code, "status": status, "message": message, **extra})

    row(
        "PYTHON",
        "OK" if supported_python() else "ERROR",
        "CPython 3.11 or 3.12 x64 required by this release",
        executable=sys.executable,
        base_executable=sys._base_executable,
        version=platform.python_version(),
        next_action="scripts/setup.ps1 -Online",
    )
    row("PLATFORM", "OK" if sys.platform == "win32" else "ERROR", "Windows x64 deployment required")
    dependencies = None
    try:
        dependencies = verify_dependencies()
        row("DEPENDENCIES", "OK", "Resolved locked gateway dependencies", versions=dependencies)
    except Exception:
        row(
            "DEPENDENCIES",
            "ERROR",
            "Locked gateway dependencies missing or incompatible",
            next_action="scripts/setup.ps1 -Online",
        )
    config, details = load_diagnostic_config(path, overrides, runtime_root)
    if config is None:
        row(
            "CONFIG",
            "ERROR",
            "配置校验失败；具体文件、字段或语法位置见 details。",
            details=details,
            next_action="修正 details 指向的文件/字段后重跑 doctor；只需 --config 主配置，声音表由 voices_file 自动读取。",
        )
        return {"status": "APP_CONFIG_INCOMPLETE", "checks": rows}
    row(
        "CONFIG",
        "OK",
        "Configuration/schema/voice identities valid",
        files={"gateway": str(config.path), "voices": str(config.voices_path)},
    )
    try:
        tokens = config.read_tokens()
        row("TOKEN", "OK", "Private token file valid", enabled_count=len(tokens))
    except ValueError as error:
        row(
            "TOKEN",
            "ERROR",
            str(error),
            next_action="Check auth.token_file and the private api-tokens.jsonc configuration",
        )
    adapter = create_adapter(config)
    try:
        adapter.validate()
        row("VOICE_ASSETS", "OK", "All registered assets exist and reference duration is valid")
    except Exception:
        row(
            "VOICE_ASSETS",
            "ERROR",
            "Voice asset/configuration validation failed",
            next_action="Configure voices.jsonc assets_root/backend_options",
        )
    try:
        (ffmpeg, ffprobe), (versions, results) = select_audio_tools(
            config, dependencies=dependencies if full else None, refresh=full
        )
        row(
            "AUDIO_TOOLS",
            "OK",
            "Selected FFmpeg/FFprobe",
            paths=[str(ffmpeg), str(ffprobe)],
            versions=versions,
        )
        if full:
            row("SIX_FORMATS", "OK", "Real foreground encoding/decoding passed", formats=results)
    except Exception:
        row(
            "AUDIO_TOOLS",
            "ERROR",
            "Audio executable/version/codec probe failed",
            next_action="scripts/setup.ps1 -Online",
        )
    if config.value.backend.mode == "auto":
        auto = config.value.backend.auto
        try:
            cwd = config.resolve(auto.working_directory)
            if (
                not config.resolve(auto.python_executable).is_file()
                or not cwd.is_dir()
                or not (cwd / "api_v2.py").is_file()
                or not config.resolve(auto.config_template).is_file()
            ):
                raise ValueError()
            args = [x for x in auto.arguments if x != "-I"]
            if auto.arguments.count("-I") > 1 or len(args) != 1 or Path(args[0]).name != "api_v2.py":
                raise ValueError()
            adapter.validate_default_port(cwd / "api_v2.py")
            row("AUTO_LAUNCH_CONFIG", "OK", "Owned launch paths valid; no process was started")
        except Exception:
            row("AUTO_LAUNCH_CONFIG", "ERROR", "Invalid auto executable/cwd/template/arguments")
    try:
        probe = await adapter.probe()
        row("BACKEND_ENDPOINT", "OK" if probe == "ready" else "WARN", "Fixed loopback GSV endpoint: " + probe)
    finally:
        await adapter.close()
    return {
        "status": "READY_FOR_STARTUP_TEST"
        if not any(x["status"] == "ERROR" for x in rows)
        else "APP_CONFIG_INCOMPLETE",
        "checks": rows,
    }
