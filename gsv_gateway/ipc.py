"""Bounded, cancellable local named-pipe JSON control. No HTTP control route."""

import asyncio
import json
import time

from .security import pipe_security


def _wait(handle, overlapped, deadline):
    import win32event
    import win32file

    remaining = max(0, int((deadline - time.monotonic()) * 1000))
    if win32event.WaitForSingleObject(overlapped.hEvent, remaining) != win32event.WAIT_OBJECT_0:
        win32file.CancelIoEx(handle, overlapped)
        # Cancellation completion must be observed before the event/buffer dies.
        win32event.WaitForSingleObject(overlapped.hEvent, 1000)
        raise TimeoutError("Control pipe deadline")
    return win32file.GetOverlappedResult(handle, overlapped, False)


def _overlapped():
    import pywintypes
    import win32event

    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    return overlapped


def _read(handle, limit, deadline):
    import win32file

    overlapped = _overlapped()
    try:
        error, buffer = win32file.ReadFile(handle, limit + 1, overlapped)
        size = _wait(handle, overlapped, deadline)
        if not 0 < size <= limit:
            raise ValueError("Control message size")
        data = json.loads(bytes(buffer[:size]))
        if not isinstance(data, dict):
            raise ValueError("Control message shape")
        return data
    finally:
        overlapped.hEvent.Close()


def _write(handle, data, limit, deadline):
    import win32file

    payload = json.dumps(data, separators=(",", ":")).encode()
    if len(payload) > limit:
        raise ValueError("Control response size")
    overlapped = _overlapped()
    try:
        win32file.WriteFile(handle, payload, overlapped)
        if _wait(handle, overlapped, deadline) != len(payload):
            raise OSError("Incomplete control write")
    finally:
        overlapped.hEvent.Close()


class ControlServer:
    def __init__(self, name, nonce, service, identity, *, startup=None):
        self.name, self.nonce, self.service, self.identity = name, nonce, service, identity
        self.startup = startup
        self.handle = None
        self.task = None
        self.closing = False

    async def start(self):
        self.loop = asyncio.get_running_loop()
        import win32con
        import win32pipe

        limit = self.service.config.value.control.max_message_bytes
        self.handle = win32pipe.CreateNamedPipe(
            self.name,
            win32pipe.PIPE_ACCESS_DUPLEX | win32con.FILE_FLAG_OVERLAPPED | 0x00080000,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT | 0x00000008,
            1,
            limit,
            limit,
            0,
            pipe_security(),
        )
        self.task = asyncio.create_task(asyncio.to_thread(self._serve))
        await asyncio.sleep(0)

    def _serve(self):
        import win32event
        import win32file
        import win32pipe

        limit = self.service.config.value.control.max_message_bytes
        timeout = self.service.config.value.control.timeout_seconds
        handle = self.handle
        try:
            while not self.closing:
                overlapped = _overlapped()
                connected = False
                try:
                    try:
                        win32pipe.ConnectNamedPipe(handle, overlapped)
                        while not self.closing:
                            if (
                                win32event.WaitForSingleObject(overlapped.hEvent, 200)
                                == win32event.WAIT_OBJECT_0
                            ):
                                win32file.GetOverlappedResult(handle, overlapped, False)
                                connected = True
                                break
                        if self.closing:
                            win32file.CancelIoEx(handle, overlapped)
                            win32event.WaitForSingleObject(overlapped.hEvent, 1000)
                    except Exception as error:
                        if getattr(error, "winerror", None) == 535:
                            connected = True
                        else:
                            raise
                    if not connected or self.closing:
                        continue
                    deadline = time.monotonic() + timeout
                    request = _read(handle, limit, deadline)
                    if set(request) != {"operation", "nonce"} or request["nonce"] != self.nonce:
                        raise ValueError("Invalid control request")
                    operation = request["operation"]
                    if operation not in {"status", "shutdown"}:
                        raise ValueError("Unknown control operation")
                    # The event loop owns state, including the shutdown event.
                    future = asyncio.run_coroutine_threadsafe(self._dispatch(operation), self.loop)
                    response = future.result(timeout=max(0.001, deadline - time.monotonic()))
                    _write(handle, response, limit, deadline)
                except Exception as error:
                    self.last_error = (type(error).__name__, getattr(error, "winerror", None))
                finally:
                    overlapped.hEvent.Close()
                    if connected:
                        try:
                            win32pipe.DisconnectNamedPipe(handle)
                        except Exception:
                            pass
        finally:
            handle.Close()
            self.handle = None

    async def _dispatch(self, operation):
        if operation == "shutdown":
            self.service.shutdown.set()
        result = self.identity | self.service.status() | {"nonce": self.nonce}
        if self.startup is not None:
            result["startup_phase"] = self.startup.get("phase")
        return result

    async def close(self):
        self.closing = True
        if self.handle:
            import win32file

            try:
                win32file.CancelIoEx(self.handle, None)
            except Exception:
                pass
        if self.task:
            await self.task


def control_request(state, operation, limit=65536, timeout=5):
    import win32con
    import win32file
    import win32pipe
    from .windows import process_matches

    if not process_matches(state["pid"], state["created"]):
        raise ValueError("Stale instance identity")
    deadline = time.monotonic() + timeout
    win32pipe.WaitNamedPipe(state["pipe"], max(1, int(timeout * 1000)))
    handle = win32file.CreateFile(
        state["pipe"],
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        0,
        None,
        win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_OVERLAPPED,
        None,
    )
    try:
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        _write(handle, {"operation": operation, "nonce": state["nonce"]}, limit, deadline)
        response = _read(handle, limit, deadline)
        if any(response.get(key) != state[key] for key in ("pid", "created", "nonce")):
            raise ValueError("Control handshake identity mismatch")
        return response
    finally:
        handle.Close()
