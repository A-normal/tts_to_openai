import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from .audio import AudioPipeline
from .backends.base import Operation
from .errors import GatewayError, unavailable


@dataclass(eq=False)
class Job:
    voice: object
    text: str
    speed: float
    format: str
    request_id: str
    buffer_chunks: int
    enqueued_at: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.delivery = asyncio.Event()
        self.delivered = False
        self.ready = asyncio.get_running_loop().create_future()
        # Observe exceptions even when a disconnected handler no longer awaits.
        self.ready.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self.output = asyncio.Queue(maxsize=self.buffer_chunks)

    async def put(self, value, timeout):
        if self.cancelled.is_set():
            return
        put = asyncio.create_task(self.output.put(value))
        cancel = asyncio.create_task(self.cancelled.wait())
        try:
            done, _ = await asyncio.wait({put, cancel}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                self.cancelled.set()
                self.fail(GatewayError("downstream_stall", "音频接收端停滞。", 504))
            if put in done:
                put.result()
        finally:
            for task in (put, cancel):
                if not task.done():
                    task.cancel()
            await asyncio.gather(put, cancel, return_exceptions=True)

    def fail(self, error):
        self.started.set()
        if not self.ready.done():
            self.ready.set_exception(error)

    def abort(self, error):
        self.fail(error)
        # Notify HTTP immediately without releasing the worker's ownership of
        # the still-draining upstream. Never append this JSON to audio bytes.
        while not self.output.empty():
            self.output.get_nowait()
        self.output.put_nowait(error)


class Scheduler:
    def __init__(self, service, pipeline_factory=AudioPipeline):
        self.service = service
        self.config = service.config.value
        self.pipeline_factory = pipeline_factory
        self.pending = deque()
        self.wake = asyncio.Event()
        self.worker = None
        self.current = None
        self.stopping = False

    def start(self):
        self.worker = asyncio.create_task(self._worker())
        self.worker.add_done_callback(self._worker_done)

    def _worker_done(self, task):
        if self.stopping or task.cancelled() or task.exception() is None:
            return
        self.service.state = "error"
        self.fail_pending()
        if self.current:
            error = unavailable("scheduler_failed")
            self.current.fail(error)
            while self.current.output.full():
                self.current.output.get_nowait()
            self.current.output.put_nowait(error)

    def submit(self, voice, text, speed, fmt, request_id):
        if self.stopping or self.service.state not in {"ready", "busy"}:
            raise unavailable()
        # Prune cancelled/expired jobs before evaluating the waiting capacity.
        now = time.monotonic()
        retained = deque()
        for job in self.pending:
            if job.cancelled.is_set() or now - job.enqueued_at >= self.config.queue.wait_timeout_seconds:
                job.fail(GatewayError("queue_timeout", "排队等待已超时。", 429))
            else:
                retained.append(job)
        self.pending = retained
        if len(self.pending) >= self.config.queue.max_pending:
            raise GatewayError("queue_full", "语音队列已满，请稍后重试。", 429)
        job = Job(voice, text, speed, fmt, request_id, max(1, self.config.audio.max_buffer_bytes // 8192))
        self.pending.append(job)
        self.wake.set()
        return job

    def fail_pending(self):
        while self.pending:
            self.pending.popleft().fail(unavailable())

    async def _worker(self):
        while not self.stopping:
            if not self.pending:
                self.wake.clear()
                await self.wake.wait()
                continue
            job = self.pending.popleft()
            if job.cancelled.is_set():
                job.fail(GatewayError("request_cancelled", "请求已取消。", 499))
                continue
            if time.monotonic() - job.enqueued_at >= self.config.queue.wait_timeout_seconds:
                job.fail(GatewayError("queue_timeout", "排队等待已超时。", 429))
                continue
            self.current = job
            job.started.set()
            operation = Operation(
                time.monotonic() + self.config.timeouts.task_total_seconds,
                job.cancelled,
                self.config.timeouts.disconnect_drain_seconds,
            )
            try:
                self.service.state = "busy"
                await self.service.adapter.prepare(job.voice, operation)
                if job.cancelled.is_set():
                    job.fail(GatewayError("request_cancelled", "请求已取消。", 499))
                    continue
                session = await self.service.adapter.synthesize(job.voice, job.text, job.speed, operation)
                pipeline = self.pipeline_factory(self.service.config, self.service.ffmpeg)

                async def emit(block, job=job, operation=operation):
                    await job.put(block, operation.remaining(self.config.timeouts.downstream_stall_seconds))
                    if not job.ready.done() and not job.cancelled.is_set():
                        job.ready.set_result(job)

                await pipeline.run(session, job.format, operation, emit, on_error=job.abort)
                if not job.cancelled.is_set():
                    await job.put(None, operation.remaining(self.config.timeouts.downstream_stall_seconds))
                    await operation.wait(job.delivery.wait(), self.config.timeouts.downstream_stall_seconds)
                if job.delivered and not job.cancelled.is_set():
                    try:
                        self.service.store.commit(job.voice)
                    except Exception:
                        self.service.state = "storage_error"
                        self.fail_pending()
                        self.service.log("snapshot_commit_failed", request_id=job.request_id)
                self.service.log(
                    "request_complete" if job.delivered else "request_cancelled",
                    request_id=job.request_id,
                    voice=job.voice.id,
                    **pipeline.metrics,
                )
            except asyncio.CancelledError:
                job.cancelled.set()
                job.fail(unavailable("stopping"))
                raise
            except BaseException as exception:
                error = (
                    exception
                    if isinstance(exception, GatewayError)
                    else GatewayError(
                        "task_failed", "语音处理失败。", uncertain=self.service.adapter.state == "uncertain"
                    )
                )
                job.fail(error)
                await job.put(error, operation.remaining(self.config.timeouts.downstream_stall_seconds))
                self.service.log(error.code, request_id=job.request_id, voice=job.voice.id)
                if error.code == "config_changed":
                    self.service.state = "config_error"
                    self.fail_pending()
                if error.uncertain or self.service.adapter.state == "uncertain":
                    self.service.adapter.state, self.service.adapter.loaded_voice = "uncertain", None
                    self.service.state = "uncertain"
                    self.fail_pending()
                    await self.service.recover()
            finally:
                if self.service.state == "busy":
                    self.service.state = "ready"
                self.current = None

    async def close(self):
        self.stopping = True
        self.fail_pending()
        self.wake.set()
        if self.worker:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.worker), self.config.timeouts.shutdown_drain_seconds
                )
            except TimeoutError:
                if self.current:
                    self.current.cancelled.set()
                if self.service.backend_process:
                    self.service.backend_process.terminate()
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
            except Exception:
                # The done callback has already failed all clients closed.
                pass
