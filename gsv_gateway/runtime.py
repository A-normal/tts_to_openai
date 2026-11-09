import asyncio
import json
import os
import uuid

import uvicorn

from .api import create_app
from .config import control_state_path
from .ipc import ControlServer
from .service import GatewayService
from .storage import write_state
from .windows import current_creation_time


async def run(config, nonce=None, *, startup=None):
    if startup is None:
        startup = {}
    nonce = nonce or uuid.uuid4().hex
    startup["phase"] = "token"
    tokens = config.read_tokens()
    startup["phase"] = "runtime_setup"
    # Authentication is available while initialization is still in progress.
    service = GatewayService(config, tokens=tokens)
    identity = {
        "pid": os.getpid(),
        "created": current_creation_time(),
        "nonce": nonce,
        "config_path": str(config.path),
        "overrides": config.overrides,
        "effective_digest": config.effective_digest,
        "runtime_root": str(config.runtime_root),
        "control": config.value.control.model_dump(),
        "shutdown_drain_seconds": config.value.timeouts.shutdown_drain_seconds,
        "pipe": "\\\\.\\pipe\\GSVGateway-" + config.instance_id,
    }
    control = ControlServer(identity["pipe"], nonce, service, identity, startup=startup)
    state_file = config.state_dir / "instance.json"
    # Uvicorn's unfiltered exception/access logs may contain request values.
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"null": {"class": "logging.NullHandler"}},
        "loggers": {
            "uvicorn": {"handlers": ["null"], "propagate": False},
            "uvicorn.error": {"handlers": ["null"], "propagate": False},
            "uvicorn.access": {"handlers": ["null"], "propagate": False},
        },
    }
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(service),
            host=config.value.server.host,
            port=config.value.server.port,
            workers=1,
            reload=False,
            access_log=False,
            log_config=log_config,
            lifespan="off",
            timeout_graceful_shutdown=config.value.timeouts.shutdown_drain_seconds,
        )
    )
    server_task = startup_task = monitor_task = None
    shutdown_task = None
    listener = None
    try:
        startup["phase"] = "instance_lock"
        service.reserve()
        # Bind once before any expensive/sensitive backend preparation. A port
        # collision must not alter weights or start an owned backend.
        import socket

        startup["phase"] = "api_listener"
        family = socket.AF_INET6 if ":" in config.value.server.host else socket.AF_INET
        listener = socket.socket(family)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((config.value.server.host, config.value.server.port))
            listener.listen(128)
            listener.setblocking(False)
            startup["phase"] = "control_pipe"
            await control.start()
            startup["phase"] = "instance_state"
            write_state(state_file, identity)
            write_state(control_state_path(config.path), identity)
            startup["phase"] = "service_start"
            server_task = asyncio.create_task(server.serve(sockets=[listener]))
            startup_task = asyncio.create_task(service.start(startup=startup))
            shutdown_task = asyncio.create_task(service.shutdown.wait())
            done, _ = await asyncio.wait(
                {startup_task, server_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if startup_task in done:
                startup_task.result()
                startup["phase"] = "ready"
                monitor_task = asyncio.create_task(service.monitor())
                print("ready", flush=True)
                await asyncio.wait({server_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
            if not startup_task.done():
                startup_task.cancel()
            shutdown_task.cancel()
            await asyncio.gather(shutdown_task, startup_task, return_exceptions=True)
        finally:
            # Uvicorn must close its accept loop before the listening socket.
            # Closing here races Windows IOCP accepts during normal shutdown.
            pass
    finally:
        service.shutdown.set()
        if startup_task and not startup_task.done():
            startup_task.cancel()
            await asyncio.gather(startup_task, return_exceptions=True)
        if monitor_task:
            await asyncio.gather(monitor_task, return_exceptions=True)
        if shutdown_task:
            shutdown_task.cancel()
            await asyncio.gather(shutdown_task, return_exceptions=True)
        try:
            await service.close()
        finally:
            try:
                await control.close()
            finally:
                server.should_exit = True
                if server_task:
                    await asyncio.gather(server_task, return_exceptions=True)
                if listener:
                    listener.close()
        for cleanup_state in (state_file, control_state_path(config.path)):
            if not cleanup_state.exists():
                continue
            try:
                if json.loads(cleanup_state.read_text())["nonce"] == nonce:
                    cleanup_state.unlink()
            except (OSError, ValueError, KeyError):
                pass
