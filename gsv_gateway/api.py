import asyncio
import hmac
import json
import math
import uuid

from fastapi import FastAPI, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response

from .audio import MIME
from .errors import GatewayError
from .jsonc import _constant, _pairs


class VoiceObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr


class Speech(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: StrictStr
    voice: StrictStr | VoiceObject
    input: StrictStr
    response_format: StrictStr = "mp3"
    speed: StrictFloat | StrictInt = 1.0
    stream_format: StrictStr = "audio"
    instructions: StrictStr = ""

    @field_validator("input")
    @classmethod
    def text(cls, value):
        if not value.strip() or len(value) > 4096:
            raise ValueError("Input must contain 1–4096 Unicode code points")
        value.encode("utf-8", errors="strict")
        return value

    @field_validator("speed")
    @classmethod
    def speed_range(cls, value):
        if not 0.25 <= value <= 4 or not math.isfinite(value):
            raise ValueError("Speed must be 0.25–4.0")
        return value


def error_response(error, request_id=None):
    headers = {"x-request-id": request_id or uuid.uuid4().hex}
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
        headers["Cache-Control"] = "no-store"
    if error.status == 429:
        headers["Retry-After"] = "1"
    return JSONResponse(error.payload(), status_code=error.status, headers=headers)


class Boundary:
    def __init__(self, app, service):
        self.app, self.service = app, service
        self.active = 0
        self.last_auth_log = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        headers = scope.get("headers", [])
        auth = [value for key, value in headers if key.lower() == b"authorization"]
        tokens = self.service.tokens
        reason = None
        if not auth:
            reason = "missing_authorization"
        elif len(auth) > 1:
            reason = "multiple_authorization"
        elif not tokens:
            reason = "token_unavailable"
        else:
            scheme, separator, credential = auth[0].partition(b" ")
            if scheme.lower() != b"bearer" or not separator:
                reason = "invalid_bearer_format"
            else:
                matched = False
                for token in tokens:
                    matched |= hmac.compare_digest(credential, token)
                if not matched:
                    reason = "token_mismatch"
        if reason:
            now = asyncio.get_running_loop().time()
            if now - self.last_auth_log.get(reason, -math.inf) >= 5:
                self.last_auth_log[reason] = now
                try:
                    self.service.log("auth_rejected", request_id=request_id, reason=reason)
                except OSError:
                    # A log write failure must not turn an authentication rejection into a 500.
                    pass
            return await error_response(
                GatewayError("invalid_api_key", "需要有效的 Bearer Token。", 401), request_id
            )(scope, receive, send)
        server = self.service.config.value.server
        if self.active >= server.max_concurrent_requests:
            return await error_response(GatewayError("ingress_full", "请求并发已满。", 429), request_id)(
                scope, receive, send
            )
        self.active += 1
        started = False
        try:
            received = 0
            body_deadline = asyncio.get_running_loop().time() + server.body_timeout_seconds

            async def bounded_receive():
                nonlocal received
                async with asyncio.timeout_at(body_deadline):
                    message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > server.max_request_bytes:
                        raise GatewayError("request_too_large", "请求正文超过大小限制。", 413)
                return message

            async def identified_send(message):
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                    message = dict(message)
                    message["headers"] = [
                        (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                    ] + [(b"x-request-id", request_id.encode())]
                await send(message)

            await self.app(scope, bounded_receive, identified_send)
        except Exception as exception:
            if started:
                self.service.log("http_stream_aborted", request_id=request_id)
                raise RuntimeError("audio_stream_aborted") from None
            error = (
                exception
                if isinstance(exception, GatewayError)
                else GatewayError(
                    "request_body_timeout" if isinstance(exception, TimeoutError) else "internal_error",
                    "请求读取超时。" if isinstance(exception, TimeoutError) else "请求处理失败。",
                    400 if isinstance(exception, TimeoutError) else 500,
                )
            )
            await error_response(error, request_id)(scope, receive, send)
        finally:
            self.active -= 1


class AudioResponse(Response):
    def __init__(self, job, service):
        # Override the ASGI lifecycle so stream failures do not emit a normal EOF.
        self.job, self.service = job, service
        super().__init__(status_code=200, media_type=MIME[job.format], headers={"Cache-Control": "no-store"})
        del self.headers["content-length"]

    async def __call__(self, scope, receive, send):
        job = self.job
        finishing = False

        async def disconnected():
            while True:
                if (await receive())["type"] == "http.disconnect":
                    if finishing:
                        await asyncio.get_running_loop().create_future()
                    return

        async def transmit():
            nonlocal finishing
            await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
            while True:
                value = await job.output.get()
                if isinstance(value, GatewayError):
                    raise RuntimeError("audio_stream_failed")
                if value is None:
                    finishing = True
                    async with asyncio.timeout(self.service.config.value.timeouts.downstream_stall_seconds):
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                    job.delivered = True
                    job.delivery.set()
                    return
                async with asyncio.timeout(self.service.config.value.timeouts.downstream_stall_seconds):
                    await send({"type": "http.response.body", "body": value, "more_body": True})

        # The body has already been consumed; the boundary's body-read deadline
        # must not apply to this long-lived disconnect watcher.
        raw_receive = scope.get("gsv.raw_receive", receive)
        receive = raw_receive
        watcher = asyncio.create_task(disconnected())
        writer = asyncio.create_task(transmit())
        try:
            done, _ = await asyncio.wait({watcher, writer}, return_when=asyncio.FIRST_COMPLETED)
            if writer in done:
                writer.result()
            else:
                job.cancelled.set()
        finally:
            if not job.delivered:
                job.cancelled.set()
                job.delivery.set()
            for task in (watcher, writer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(watcher, writer, return_exceptions=True)


def create_app(service):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(GatewayError)
    async def gateway_error(request, error):
        return error_response(error, request.state.request_id)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return error_response(
            GatewayError(
                "not_found" if error.status_code == 404 else "method_not_allowed",
                "路径不存在。" if error.status_code == 404 else "方法不允许。",
                error.status_code,
            ),
            request.state.request_id,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        return error_response(GatewayError("internal_error", "请求处理失败。", 500), request.state.request_id)

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [service.adapter.describe()]}

    @app.get("/v1/models/{model}")
    async def model(model: str):
        if model != service.adapter.describe()["id"]:
            raise GatewayError("model_not_found", "模型不存在。", 404, "model")
        return service.adapter.describe()

    @app.get("/voices")
    async def voices():
        return {"voices": service.config.public_voices()}

    @app.get("/health/live")
    async def live():
        return {"live": True}

    @app.get("/health/ready")
    async def ready():
        status = service.status()
        return JSONResponse(status, status_code=200 if status["ready"] else 503)

    @app.post("/v1/audio/speech")
    async def speech(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise GatewayError("invalid_content_type", "请求必须使用 application/json。", 400)
        try:
            raw = json.loads(await request.body(), object_pairs_hook=_pairs, parse_constant=_constant)
            data = Speech.model_validate(raw)
        except ValidationError as error:
            param = str(error.errors()[0]["loc"][0]) if error.errors()[0]["loc"] else None
            raise GatewayError(
                "invalid_parameter", "请求字段缺失、类型错误或超出允许范围。", 400, param
            ) from None
        except (ValueError, UnicodeError):
            raise GatewayError("invalid_json", "请求 JSON 无效。", 400) from None
        if data.model != service.adapter.describe()["id"]:
            raise GatewayError("model_not_found", "模型不存在。", 404, "model")
        for param, allowed in [
            ("response_format", MIME),
            ("stream_format", {"audio"}),
            ("instructions", {""}),
        ]:
            if getattr(data, param) not in allowed:
                raise GatewayError("unsupported_parameter", "此参数值不受支持。", 400, param)
        try:
            voice = service.config.registry.find(data.voice if isinstance(data.voice, str) else data.voice.id)
        except KeyError:
            raise GatewayError("voice_not_found", "声音不存在。", 400, "voice") from None
        job = service.scheduler.submit(
            voice, data.input, data.speed, data.response_format, request.state.request_id
        )
        request.scope["gsv.raw_receive"] = request.scope["gsv.original_receive"]

        async def wait_start():
            async with asyncio.timeout(service.config.value.queue.wait_timeout_seconds):
                await job.started.wait()
            return await asyncio.shield(job.ready)

        async def disconnect():
            while (await request.scope["gsv.original_receive"]())["type"] != "http.disconnect":
                pass
            job.cancelled.set()

        waiter = asyncio.create_task(wait_start())
        watcher = asyncio.create_task(disconnect())
        try:
            done, _ = await asyncio.wait({waiter, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                job.cancelled.set()
                return Response(status_code=499)
            try:
                await waiter
            except TimeoutError:
                job.cancelled.set()
                raise GatewayError("queue_timeout", "排队等待已超时。", 429) from None
            return AudioResponse(job, service)
        finally:
            if not waiter.done():
                job.cancelled.set()
                waiter.cancel()
            watcher.cancel()
            await asyncio.gather(waiter, watcher, return_exceptions=True)

    boundary = Boundary(app, service)

    async def application(scope, receive, send):
        if scope["type"] == "http":
            scope["gsv.original_receive"] = receive
        await boundary(scope, receive, send)

    return application
