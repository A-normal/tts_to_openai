import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path

from .config import LoadedConfig, control_state_path
from .errors import GatewayError
from .ipc import control_request
from .security import require_private
from .windows import (
    detached_gateway,
    job_has_processes,
    process_matches,
    release_startup_job,
    startup_instance_matches,
    wait_job_empty,
)


def parser():
    result = argparse.ArgumentParser(
        description="Local OpenAI Speech gateway (GSV backend on 127.0.0.1:9880)"
    )
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("run", "start", "stop", "restart", "status", "voices", "doctor", "check-config"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", required=True, type=Path)
        sub.add_argument("--runtime-root", type=Path)
        if name in {"run", "start", "restart"}:
            sub.add_argument("--host")
            sub.add_argument("--port", type=int)
            sub.add_argument("--backend-mode", choices=["connect", "auto"])
            sub.add_argument("--token-file")
        if name in {"status", "doctor", "check-config", "voices"}:
            sub.add_argument("--json", action="store_true")
        if name == "restart":
            sub.add_argument("--reset-overrides", action="store_true")
        if name == "run":
            sub.add_argument("--startup-nonce", help=argparse.SUPPRESS)
    return result


def _overrides(args):
    return {
        key: getattr(args, field)
        for field, key in [
            ("host", "server.host"),
            ("port", "server.port"),
            ("backend_mode", "backend.mode"),
            ("token_file", "auth.token_file"),
        ]
        if getattr(args, field, None) is not None
    }


def _state(config):
    return _state_from_path(config.path)


def _state_from_path(config_path):
    path = control_state_path(config_path)
    if not path.is_file():
        return None
    require_private(path)
    if path.stat().st_size > 65536:
        raise ValueError("Invalid local state")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("config_path") != str(config_path.resolve()):
        raise ValueError("Instance configuration identity mismatch")
    return state if process_matches(state["pid"], state["created"]) else None


def _status(config, state):
    control = state.get("control", {"max_message_bytes": 65536, "timeout_seconds": 5})
    return control_request(
        state, "status", min(65536, control["max_message_bytes"]), min(60, control["timeout_seconds"])
    )


_STARTUP_MESSAGES = {
    "startup_logging": "Preparing private runtime files and logs",
    "environment": "Checking the Python environment and audio tools",
    "voice_assets": "Validating voice assets",
    "snapshot_selection": "Selecting the startup voice",
    "backend_probe": "Checking the GPT-SoVITS endpoint",
    "backend_launch": "Waiting for managed GPT-SoVITS to start",
    "startup_self_test": "Running the FLAC synthesis and decode self-test",
}


def _progress(message):
    print(f"[gateway] {message}", file=sys.stderr, flush=True)


def _stop(config, state):
    if state is None:
        return
    _progress("Requesting shutdown and waiting for the running gateway to exit.")
    control = state.get("control", {"max_message_bytes": 65536, "timeout_seconds": 5})
    control_request(
        state, "shutdown", min(65536, control["max_message_bytes"]), min(60, control["timeout_seconds"])
    )
    deadline = time.monotonic() + 2 * min(600, state.get("shutdown_drain_seconds", 30)) + 15
    while time.monotonic() < deadline:
        if not process_matches(state["pid"], state["created"]):
            return
        time.sleep(0.1)
    raise RuntimeError("Old instance has not exited; no replacement was started")


def _startup_failure_path(config_path, nonce):
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise ValueError("Invalid startup nonce")
    return control_state_path(config_path).with_suffix(f".{nonce}.failure.json")


def _record_startup_failure(path, phase, error):
    data = {"phase": phase, "reason": error.code if isinstance(error, GatewayError) else type(error).__name__}
    for key in ("winerror", "errno"):
        value = getattr(error, key, None)
        if type(value) is int:
            data[key] = value
    try:
        from .storage import write_state

        write_state(path, data)
    except Exception:
        # The launcher can still report the process exit code if the file is unavailable.
        pass


def _startup_exit_error(handle, failure_path):
    import pywintypes
    import win32process

    exit_code = win32process.GetExitCodeProcess(handle)
    details = [f"exit_code={exit_code}"]
    try:
        require_private(failure_path)
        with failure_path.open("rb") as stream:
            data = json.loads(stream.read(1024))
        for key in ("phase", "reason"):
            value = data.get(key)
            if isinstance(value, str) and re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]{0,63}", value):
                details.append(f"{key}={value}")
        for key in ("winerror", "errno"):
            value = data.get(key)
            if type(value) is int:
                details.append(f"{key}={value}")
    except (OSError, ValueError, AttributeError, pywintypes.error):
        pass
    return GatewayError("startup_exited", "Gateway initialization exited (" + ", ".join(details) + ")")


def _start(config):
    started = time.monotonic()
    last_message, last_notice = None, started

    def report(message):
        nonlocal last_message, last_notice
        now = time.monotonic()
        if message != last_message or now - last_notice >= 10:
            _progress(f"{message} ({now - started:.0f}s elapsed).")
            last_message, last_notice = message, now

    state = _state(config)
    if state:
        report("Checking the running gateway")
        response = _status(config, state)
        if state["effective_digest"] != config.effective_digest:
            raise ValueError("Running configuration differs; use restart")
        deadline = time.monotonic() + config.value.timeouts.startup_seconds
        while not response["ready"]:
            report(
                _STARTUP_MESSAGES.get(
                    response.get("startup_phase"), "Waiting for the gateway to become ready"
                )
            )
            if (
                response["state"]
                in {
                    "error",
                    "storage_error",
                    "log_error",
                    "config_error",
                    "manual_recovery_required",
                    "stopping",
                }
                or time.monotonic() >= deadline
            ):
                raise RuntimeError("Existing instance is not ready")
            time.sleep(0.1)
            response = _status(config, state)
        print("already_running")
        return
    nonce = uuid.uuid4().hex
    failure_path = _startup_failure_path(config.path, nonce)
    arguments = [
        "-m",
        "gsv_gateway",
        "run",
        "--config",
        str(config.path),
        "--runtime-root",
        str(config.runtime_root),
        "--startup-nonce",
        nonce,
    ]
    for key, value in config.overrides.items():
        flag = {
            "server.host": "--host",
            "server.port": "--port",
            "backend.mode": "--backend-mode",
            "auth.token_file": "--token-file",
        }[key]
        arguments.extend([flag, str(value)])
    report("Launching the gateway process")
    handle, _, _, startup_job = detached_gateway(arguments, config.runtime_root.parent)
    success = False
    try:
        deadline = time.monotonic() + config.value.timeouts.startup_seconds
        while time.monotonic() < deadline:
            if not job_has_processes(startup_job):
                raise _startup_exit_error(handle, failure_path)
            state = _state(config)
            if state:
                if not startup_instance_matches(state, nonce, startup_job):
                    raise GatewayError(
                        "startup_identity_conflict", "Startup process identity verification failed"
                    )
                if state["effective_digest"] != config.effective_digest:
                    raise GatewayError(
                        "startup_config_changed", "Configuration changed during startup; retry start"
                    )
                try:
                    status = _status(config, state)
                    if status["ready"]:
                        # The temporary startup Job must not kill a successfully
                        # handed-off gateway when the short-lived launcher exits.
                        release_startup_job(startup_job)
                        success = True
                        report("Gateway ready")
                        print("ready")
                        return
                    report(
                        _STARTUP_MESSAGES.get(
                            status.get("startup_phase"), "Waiting for the gateway to become ready"
                        )
                    )
                except OSError:
                    pass
            else:
                report("Waiting for the gateway control channel")
            time.sleep(0.1)
        raise GatewayError("startup_timeout", "Startup deadline exceeded; inspect private startup log")
    finally:
        try:
            if not success:
                import win32job

                win32job.TerminateJobObject(startup_job, 1)
                asyncio.run(wait_job_empty(startup_job, config.value.timeouts.shutdown_drain_seconds))
        finally:
            startup_job.Close()
            handle.Close()
            try:
                failure_path.unlink(missing_ok=True)
            except OSError:
                pass


def main(argv=None):
    args = parser().parse_args(argv)
    failure_path = None
    startup = {"phase": "configuration"}
    try:
        if args.command == "run" and args.startup_nonce is not None:
            failure_path = _startup_failure_path(args.config, args.startup_nonce)
        if args.command in {"doctor", "check-config"}:
            from .doctor import diagnose

            report = asyncio.run(diagnose(args.config, runtime_root=args.runtime_root))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "READY_FOR_STARTUP_TEST" else 2
        # Stop/status must remain available after an invalid edit or deletion of
        # the config/registry. Identity is canonical path + live IPC handshake.
        state = _state_from_path(args.config) if args.command in {"status", "stop", "restart"} else None
        if args.command in {"status", "stop"}:
            if args.command == "stop":
                _stop(None, state)
                print("stopped")
                return 0
            if state is None:
                print(json.dumps({"state": "not_running"}) if args.json else "not_running")
                return 4
            status = _status(None, state)
            public = {
                k: v
                for k, v in status.items()
                if k not in {"nonce", "config_path", "overrides", "runtime_root", "pipe"}
            }
            print(json.dumps(public, ensure_ascii=False, indent=2) if args.json else status["state"])
            return 0 if status["ready"] else 3
        if args.command == "restart":
            prior = {} if args.reset_overrides or state is None else dict(state["overrides"])
            prior.update(_overrides(args))
            runtime_root = args.runtime_root or (Path(state["runtime_root"]) if state else None)
            config = LoadedConfig(args.config, prior, runtime_root)
        else:
            config = LoadedConfig(args.config, _overrides(args), args.runtime_root)
        if args.command == "voices":
            print(
                json.dumps(
                    {"voices": [v.public() for v in config.registry.voices]}, ensure_ascii=False, indent=2
                )
            )
            return 0
        if args.command == "run":
            startup["phase"] = "runtime_import"
            from .runtime import run

            asyncio.run(run(config, args.startup_nonce, startup=startup))
            return 0
        if args.command == "restart":
            _stop(config, state)
            if state is not None:
                _progress("Previous gateway stopped.")
        _start(config)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        if failure_path and startup["phase"] != "ready":
            _record_startup_failure(failure_path, startup["phase"], error)
        if isinstance(error, (ValueError, FileNotFoundError)):
            # Validation errors contain source values; never dump the exception.
            print(
                "configuration_error: check JSONC, assets, permissions and explicit arguments",
                file=sys.stderr,
            )
            return 2
        print(
            f"{error.code}: {error.message}"
            if isinstance(error, GatewayError)
            else "operation_failed: inspect doctor and private startup log",
            file=sys.stderr,
        )
        return 1
