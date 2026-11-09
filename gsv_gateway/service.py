import asyncio
import json
import logging
import logging.handlers
import os
import tempfile
import time
import uuid
from pathlib import Path

from .audio import AudioPipeline, decode_check
from .backends import create_adapter
from .backends.base import Operation
from .errors import GatewayError
from .scheduler import Scheduler
from .security import secure_path
from .storage import SnapshotStore
from .windows import ManagedProcess, Mutex


class GatewayService:
    def __init__(self, config, adapter=None, *, tokens: tuple[bytes, ...] = ()):
        self.config = config
        self.tokens = tokens
        self.adapter = adapter or create_adapter(config)
        self.state = "starting"
        self.backend_process = None
        self.backend_drains = []
        self.owned = False
        self.recovery_attempts = 0
        self.shutdown = asyncio.Event()
        self.scheduler = Scheduler(self)
        self.store = SnapshotStore(config, self.adapter)
        self.ffmpeg = self.ffprobe = None
        self.mutexes = []
        self.logger = None

    def log(self, code, **values):
        if self.logger:
            level = logging.INFO if code in {"request_complete", "request_cancelled"} else logging.ERROR
            if code == "auth_rejected":
                level = logging.WARNING
            self.logger.log(level, json.dumps({"code": code, **values}, ensure_ascii=False))

    def configure_logs(self):
        directory = self.config.resolve(self.config.value.logging.directory)
        secure_path(directory, directory=True)
        self.logger = logging.getLogger("gsv_gateway." + self.config.instance_id)
        self.logger.propagate = False
        self.logger.setLevel(self.config.value.logging.level)
        self.logger.handlers.clear()
        for name in ("gateway.log", "startup.log"):
            handler = logging.handlers.RotatingFileHandler(
                directory / name,
                maxBytes=self.config.value.logging.rotate_bytes,
                backupCount=self.config.value.logging.backup_count,
                encoding="utf-8",
            )
            # Logging normally swallows write errors. Startup success must fail
            # closed instead, so use an exception-propagating handler below.
            handler.handleError = _raise_logging_error
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            if name == "gateway.log":
                self.logger.addHandler(handler)
            else:
                self.startup_handler = handler

    def startup_log(self, code, **values):
        record = logging.LogRecord(
            "startup", logging.INFO, "", 0, json.dumps({"code": code, **values}, ensure_ascii=False), (), None
        )
        try:
            self.startup_handler.handle(record)
            self.startup_handler.flush()
        except OSError:
            self.state = "log_error"
            raise

    async def start(self, *, startup=None):
        if startup is None:
            startup = {}
        start = time.monotonic()
        deadline = start + self.config.value.timeouts.startup_seconds
        startup["phase"] = "startup_logging"
        secure_path(self.config.state_dir, directory=True)
        self.configure_logs()
        if not self.tokens:
            self.tokens = self.config.read_tokens()
        if not self.mutexes:
            self.reserve()
        async with asyncio.timeout_at(deadline):
            from .environment import validate_environment

            startup["phase"] = "environment"
            self.ffmpeg, self.ffprobe, self.tool_versions = await asyncio.to_thread(
                validate_environment, self.config
            )
            startup["phase"] = "voice_assets"
            await asyncio.to_thread(self.adapter.validate)
            startup["phase"] = "snapshot_selection"
            voice, source = self.store.select()
            startup["phase"] = "backend_probe"
            probe = await self.adapter.probe()
            if probe == "unreachable" and self.config.value.backend.mode == "auto":
                startup["phase"] = "backend_launch"
                await self.launch_backend(voice, deadline)
            elif probe != "ready":
                raise GatewayError("backend_not_ready", "后端未运行或接口不兼容。", 503)
            startup["phase"] = "startup_self_test"
            await self.self_test(voice, source, deadline)
        self.scheduler.start()
        self.state = "ready"

    def reserve(self):
        self.mutexes.append(Mutex("instance-" + self.config.instance_id))
        self.mutexes.append(Mutex("backend-gpt-sovits-127.0.0.1-9880"))

    async def launch_backend(self, voice, deadline):
        if self.backend_process is not None:
            raise RuntimeError("Old owned process must exit before relaunch")
        spec = await asyncio.to_thread(self.adapter.build_launch_spec, voice)
        self.backend_process = ManagedProcess(spec)
        self.backend_process.end_input()
        self.owned = True

        async def discard(which):
            while await self.backend_process.read(which):
                pass

        self.backend_drains = [asyncio.create_task(discard(which)) for which in ("stdout", "stderr")]
        while time.monotonic() < deadline:
            if self.backend_process.returncode is not None:
                raise GatewayError("backend_start_failed", "受管后端初始化失败。", 503)
            if await self.adapter.probe() == "ready":
                self.adapter.state, self.adapter.loaded_voice = "unknown_clean", None
                return
            await asyncio.sleep(0.2)
        raise TimeoutError()

    async def stop_backend(self):
        if self.backend_process:
            self.backend_process.terminate()
            await self.backend_process.wait_tree(self.config.value.timeouts.shutdown_drain_seconds)
            await asyncio.gather(*self.backend_drains, return_exceptions=True)
            self.backend_process.close()
            self.backend_process = None
            self.backend_drains = []

    async def self_test(self, voice, source, deadline):
        startup_id = uuid.uuid4().hex
        started = time.monotonic()
        operation = Operation(min(deadline, started + self.config.value.timeouts.task_total_seconds))
        self.startup_log(
            "startup_test_begin",
            startup_id=startup_id,
            test_id="FLAC-01",
            voice=voice.id,
            snapshot_source=source,
        )
        path = None
        phases = {}
        try:
            await self.adapter.prepare(voice, operation)
            phases["prepare_seconds"] = round(time.monotonic() - started, 3)
            phase_start = time.monotonic()
            session = await self.adapter.synthesize(
                voice, self.config.value.startup_test.text, 1.0, operation
            )
            phases["first_pcm_seconds"] = round(time.monotonic() - phase_start, 3)
            fd, name = tempfile.mkstemp(suffix=".flac", dir=self.config.state_dir)
            path = Path(name)
            with os.fdopen(fd, "wb") as output:

                async def receive(block):
                    output.write(block)

                pipeline = AudioPipeline(self.config, self.ffmpeg)
                await pipeline.run(session, "flac", operation, receive)
            if not session.non_silent:
                raise GatewayError("startup_silent", "启动自检返回了全静音。")
            decoded = await decode_check(
                self.ffmpeg, path, session.samples, session.sample_rate, operation.deadline
            )
            try:
                self.store.commit(voice)
            except OSError:
                self.state = "storage_error"
                raise GatewayError("snapshot_commit_failed", "成功快照无法持久化。", 503) from None
            self.startup_log(
                "startup_test_success",
                startup_id=startup_id,
                test_id="FLAC-01",
                voice=voice.id,
                snapshot_source=source,
                elapsed_seconds=round(time.monotonic() - started, 3),
                tool_versions=getattr(self, "tool_versions", []),
                phases=phases,
                **pipeline.metrics,
                **decoded,
            )
        except BaseException as exc:
            self.startup_log(
                "startup_test_failed",
                startup_id=startup_id,
                failure_code=exc.code if isinstance(exc, GatewayError) else "startup_failure",
            )
            raise
        finally:
            if path:
                path.unlink(missing_ok=True)

    async def recover(self):
        if not self.owned:
            self.state = "manual_recovery_required"
            return
        self.state = "recovering"
        recovery = self.config.value.backend.recovery
        while self.recovery_attempts < recovery.max_attempts and not self.shutdown.is_set():
            delay = recovery.backoff_seconds[self.recovery_attempts]
            self.recovery_attempts += 1
            try:
                await asyncio.wait_for(self.shutdown.wait(), delay)
                return
            except TimeoutError:
                pass
            try:
                deadline = time.monotonic() + self.config.value.timeouts.startup_seconds
                async with asyncio.timeout_at(deadline):
                    await self.stop_backend()
                    voice, source = self.store.select()
                    await self.launch_backend(voice, deadline)
                    await self.self_test(voice, source, deadline)
                self.state = "ready"
                return
            except Exception:
                if self.state in {"storage_error", "log_error"}:
                    return
                self.log("backend_recovery_failed", attempt=self.recovery_attempts)
        self.state = "error"

    async def monitor(self):
        while not self.shutdown.is_set():
            if self.owned and self.backend_process and self.backend_process.returncode is not None:
                # The single consumer owns busy-task failures and their recovery.
                if self.scheduler.current is None and self.state == "ready":
                    self.state = "recovering"
                    self.scheduler.fail_pending()
                    await self.recover()
            try:
                await asyncio.wait_for(self.shutdown.wait(), 0.5)
            except TimeoutError:
                pass

    def status(self):
        return {
            "state": self.state,
            "ready": self.state in {"ready", "busy"},
            "busy": self.scheduler.current is not None,
            "backend_owned": self.owned,
            "backend_state": self.adapter.state,
            "voice": self.adapter.loaded_voice,
            "pending": len(self.scheduler.pending),
            "host": self.config.value.server.host,
            "port": self.config.value.server.port,
            "recovery_attempts": self.recovery_attempts,
        }

    async def close(self):
        self.state = "stopping"
        self.shutdown.set()
        await self.scheduler.close()
        await self.stop_backend()
        await self.adapter.close()
        for mutex in reversed(self.mutexes):
            mutex.close()
        self.mutexes.clear()
        if self.logger:
            for handler in self.logger.handlers:
                handler.close()
            self.logger.handlers.clear()
        if hasattr(self, "startup_handler"):
            self.startup_handler.close()


def _raise_logging_error(_record):
    raise OSError("Log write failed")
