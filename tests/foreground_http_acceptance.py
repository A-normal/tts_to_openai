"""Bounded real HTTP/SDK/curl/proxy acceptance in ONE foreground process.

No detached gateway/backend process. Native FFmpeg children have their hiding
flag disabled and are synchronously owned/reaped by the foreground gateway.
"""

import asyncio
import json
import socket
import subprocess
import time
from pathlib import Path

import httpx
import uvicorn
import win32process
from openai import AsyncOpenAI

from gsv_gateway.config import LoadedConfig, control_state_path
from gsv_gateway.ipc import control_request
from gsv_gateway.runtime import run
from gsv_gateway.security import secure_path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def main():
    win32process.CREATE_NO_WINDOW = 0
    original = LoadedConfig(Path("config/gateway.jsonc"))
    root = Path(".private/development/records/acceptance/foreground-http-2026-11-09-2").resolve()
    secure_path(root, directory=True)
    config_path = root / "gateway.jsonc"
    if config_path.exists():
        raise ValueError("Evidence already exists; do not replay blindly")
    raw = original.value.model_dump()
    raw["auth"]["token_file"] = str(original.token_path)
    raw["voices_file"] = str(original.voices_path)
    raw["runtime_dir"] = str(root / "data")
    raw["logging"]["directory"] = str(root / "data/logs")
    raw["backend"]["mode"] = "connect"
    raw["backend"]["last_good_config"] = str(root / "data/backend/last-good.yaml")
    raw["backend"]["auto"]["runtime_config"] = str(root / "data/backend/tts-infer.yaml")
    raw["server"]["port"] = free_port()
    config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    config = LoadedConfig(config_path, runtime_root=original.runtime_root)
    token = config.read_token()
    url = f"http://127.0.0.1:{config.value.server.port}"
    task = asyncio.create_task(run(config))
    proxy_task = None
    proxy_server = None
    report = {
        "scope": "real foreground run + external GSV + native foreground FFmpeg + SDK/curl/streaming proxy",
        "background_processes_started": False,
    }
    headers_file = root / "temporary-headers.txt"
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=180
        ) as http:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if task.done():
                    task.result()
                    raise RuntimeError("Gateway exited before readiness")
                try:
                    response = await http.get(url + "/health/ready", timeout=2)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
            else:
                raise TimeoutError("Startup gate did not complete")
            report["startup_ready"] = True
            report["unauthenticated_http"] = (
                await http.get(url + "/v1/models", headers={"Authorization": "Bearer wrong"})
            ).status_code
            print(
                json.dumps(
                    {"event": "foreground_ready", "unauthenticated_http": report["unauthenticated_http"]}
                ),
                flush=True,
            )
            sdk = AsyncOpenAI(api_key=token, base_url=url + "/v1", http_client=http, max_retries=0)
            report["sdk_models"] = [item.id for item in (await sdk.models.list()).data]
            async with sdk.audio.speech.with_streaming_response.create(
                model="gpt-sovits",
                voice="aimisi",
                input="你好，这是 SDK 测试。现在检查连续语音返回。",
                response_format="mp3",
                stream_format="audio",
            ) as response:
                result = await response.read()
                (root / "sdk.mp3").write_bytes(result)
                report["sdk_speech_bytes"] = len(result)
            headers_file.write_text("Authorization: Bearer " + token, encoding="utf-8")
            secure_path(headers_file)
            request_file = root / "request.json"
            request_file.write_text(
                json.dumps(
                    {
                        "model": "gpt-sovits",
                        "voice": "aimisi",
                        "input": "你好，这是 curl 测试。现在检查完整音频返回。",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            curl = await asyncio.to_thread(
                subprocess.run,
                [
                    "curl.exe",
                    "--silent",
                    "--show-error",
                    "--no-buffer",
                    "--max-time",
                    "180",
                    "-H",
                    "@" + str(headers_file),
                    "-H",
                    "Content-Type: application/json",
                    "--data-binary",
                    "@" + str(request_file),
                    "--output",
                    str(root / "curl.mp3"),
                    url + "/v1/audio/speech",
                ],
                capture_output=True,
                timeout=190,
            )
            report["curl_exit"] = curl.returncode
            assert curl.returncode == 0 and (root / "curl.mp3").stat().st_size > 100

            async def proxy(scope, receive, send):
                if scope["type"] != "http":
                    return
                body = bytearray()
                while True:
                    message = await receive()
                    body.extend(message.get("body", b""))
                    if not message.get("more_body", False):
                        break
                forwarded = [
                    (key.decode(), value.decode())
                    for key, value in scope["headers"]
                    if key.lower() in {b"authorization", b"content-type"}
                ]
                async with httpx.AsyncClient(trust_env=False, timeout=180) as upstream:
                    async with upstream.stream(
                        scope["method"], url + scope["path"], headers=forwarded, content=bytes(body)
                    ) as response:
                        keep = {b"content-type", b"cache-control", b"x-request-id"}
                        response_headers = [
                            (k.encode(), v.encode())
                            for k, v in response.headers.items()
                            if k.encode() in keep
                        ]
                        await send(
                            {
                                "type": "http.response.start",
                                "status": response.status_code,
                                "headers": response_headers,
                            }
                        )
                        async for block in response.aiter_raw():
                            await send({"type": "http.response.body", "body": block, "more_body": True})
                        await send({"type": "http.response.body", "body": b"", "more_body": False})

            port = free_port()
            proxy_server = uvicorn.Server(
                uvicorn.Config(
                    proxy, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="critical"
                )
            )
            proxy_task = asyncio.create_task(proxy_server.serve())
            while not proxy_server.started:
                if proxy_task.done():
                    proxy_task.result()
                await asyncio.sleep(0.05)
            first = None
            async with http.stream(
                "POST",
                f"http://127.0.0.1:{port}/v1/audio/speech",
                json={
                    "model": "gpt-sovits",
                    "voice": "aimisi",
                    "input": "你好，这是第一段测试。随后检查反向代理是否及时传递后续语音。",
                },
            ) as response:
                request_id = response.headers["x-request-id"]
                assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
                with (root / "proxy.mp3").open("wb") as sink:
                    async for block in response.aiter_raw():
                        if first is None:
                            first = time.monotonic()
                        sink.write(block)
            log = config.resolve(config.value.logging.directory) / "gateway.log"
            end = time.monotonic() + 3
            matching = None
            while matching is None and time.monotonic() < end:
                for line in log.read_text(encoding="utf-8").splitlines():
                    data = json.loads(line[line.index("{") :])
                    if data.get("request_id") == request_id and data["code"] == "request_complete":
                        matching = data
                await asyncio.sleep(0.01)
            assert matching and first < matching["upstream_eof_monotonic"]
            report["proxy_first_audio_before_upstream_eof"] = True
            report["proxy_margin_seconds"] = round(matching["upstream_eof_monotonic"] - first, 3)
            state = json.loads(control_state_path(config.path).read_text())
            status = await asyncio.to_thread(control_request, state, "status")
            report["native_control_ready"] = status["ready"]
            report["backend_owned"] = status["backend_owned"]
            await asyncio.to_thread(control_request, state, "shutdown")
            await asyncio.wait_for(task, 45)
            report["foreground_shutdown_complete"] = True
            report["complete"] = True
    finally:
        headers_file.unlink(missing_ok=True)
        if proxy_server:
            proxy_server.should_exit = True
        if proxy_task:
            await proxy_task
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        (root / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
