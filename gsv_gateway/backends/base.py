import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Protocol

from ..windows import LaunchSpec


@dataclass
class Operation:
    deadline: float
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    drain_seconds: float = 120
    cancelled_at: float | None = None

    def begin_drain(self):
        if self.cancelled_at is None:
            self.cancelled_at = time.monotonic()

    def remaining(self, phase: float) -> float:
        end = self.deadline
        if self.cancelled.is_set() or self.cancelled_at is not None:
            if self.cancelled_at is None:
                self.cancelled_at = time.monotonic()
            end = min(end, self.cancelled_at + self.drain_seconds)
        return max(0.001, min(phase, end - time.monotonic()))

    async def wait(self, awaitable, phase):
        # Re-evaluate the cancellation deadline without cancelling the upstream
        # operation merely because the HTTP handler went away.
        task = asyncio.ensure_future(awaitable)
        phase_end = time.monotonic() + phase
        try:
            while True:
                timeout = self.remaining(max(0.001, phase_end - time.monotonic()))
                if time.monotonic() >= min(phase_end, self.deadline):
                    raise TimeoutError()
                done, _ = await asyncio.wait({task}, timeout=min(timeout, 0.2))
                if done:
                    return task.result()
                if (
                    self.cancelled_at is not None
                    and time.monotonic() >= self.cancelled_at + self.drain_seconds
                ):
                    raise TimeoutError()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@dataclass
class AudioSession:
    sample_rate: int
    pcm: AsyncIterator[bytes]
    complete: bool = False
    samples: int = 0
    non_silent: bool = False
    first_pcm_monotonic: float | None = None


class BackendAdapter(Protocol):
    reservation_key: str
    endpoint_label: str
    state: str
    loaded_voice: str | None

    def describe(self) -> dict: ...
    def validate(self) -> None: ...
    async def probe(self) -> Literal["ready", "unreachable", "uncertain", "incompatible"]: ...
    async def prepare(self, voice, operation: Operation): ...
    async def synthesize(self, voice, text: str, speed: float, operation: Operation) -> AudioSession: ...
    def snapshot(self, voice) -> dict: ...
    async def close(self): ...


class ManagedBackendAdapter(BackendAdapter, Protocol):
    def validate_launch(self) -> None: ...
    def build_launch_spec(self, voice) -> LaunchSpec: ...
