import asyncio

import httpx
import pytest

from gsv_gateway.api import create_app


@pytest.fixture
async def client(service):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + service.tokens[0].decode("ascii")},
    ) as client:
        yield client


@pytest.mark.parametrize("path", ["/v1/models", "/voices", "/health/live", "/health/ready", "/missing"])
async def test_all_paths_require_auth(client, path):
    response = await client.get(path, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert "x-request-id" in response.headers


async def test_auth_rejection_diagnostics_are_redacted_and_rate_limited(client, service):
    events = []
    service.log = lambda code, **values: events.append((code, values))
    authorization = client.headers.pop("Authorization")
    first = await client.get("/v1/models")
    assert first.status_code == 401
    assert (await client.get("/v1/models")).status_code == 401
    wrong = await client.get("/v1/models", headers={"Authorization": "Bearer private-wrong-value"})
    duplicate = await client.get("/v1/models", headers=[("Authorization", authorization)] * 2)
    assert wrong.status_code == duplicate.status_code == 401
    assert (await client.get("/v1/models", headers={"Authorization": authorization})).status_code == 200
    assert events == [
        ("auth_rejected", {"request_id": first.headers["x-request-id"], "reason": "missing_authorization"}),
        ("auth_rejected", {"request_id": wrong.headers["x-request-id"], "reason": "token_mismatch"}),
        (
            "auth_rejected",
            {"request_id": duplicate.headers["x-request-id"], "reason": "multiple_authorization"},
        ),
    ]


async def test_models_and_private_voice_fields(client):
    response = await client.get("/v1/models")
    assert [x["id"] for x in response.json()["data"]] == ["gpt-sovits"]
    assert (await client.get("/v1/models/gpt-sovits")).json() == response.json()["data"][0]
    assert (await client.get("/v1/models/missing")).status_code == 404
    assert "backend_options" not in (await client.get("/voices")).text
    assert (await client.post("/v1/models")).status_code == 405


@pytest.mark.parametrize(
    "fields,param",
    [
        ({"speed": True}, "speed"),
        ({"speed": "1"}, "speed"),
        ({"speed": 0.24}, "speed"),
        ({"speed": 4.01}, "speed"),
        ({"input": " "}, "input"),
        ({"input": "a" * 4097}, "input"),
        ({"voice": None}, "voice"),
        ({"stream": True}, "stream"),
        ({"stream_format": "sse"}, "stream_format"),
        ({"instructions": "happy"}, "instructions"),
        ({"response_format": None}, "response_format"),
        ({"voice": {"id": "a", "extra": 1}}, "voice"),
    ],
)
async def test_strict_request_validation(client, service, fields, param):
    response = await client.post(
        "/v1/audio/speech", json={"model": "gpt-sovits", "voice": "a", "input": "test"} | fields
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["param"] == param
    assert not service.adapter.events


async def test_chunked_body_real_limit(client, service):
    async def body():
        for _ in range(65):
            yield b" " * 1024

    response = await client.post(
        "/v1/audio/speech", content=body(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert not service.adapter.events


async def test_default_mp3_success_and_snapshot(client, service):
    response = await client.post(
        "/v1/audio/speech",
        json={"model": "gpt-sovits", "voice": {"id": "a"}, "input": "test", "instructions": ""},
    )
    assert response.status_code == 200, response.text[:100]
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["cache-control"] == "no-store"
    assert "content-length" not in response.headers
    assert len(response.content) > 100
    async with asyncio.timeout(2):
        while service.scheduler.current is not None:
            await asyncio.sleep(0)
    assert service.config.snapshot_path.exists()


async def test_not_ready_is_503(client, service):
    service.state = "recovering"
    response = await client.post(
        "/v1/audio/speech", json={"model": "gpt-sovits", "voice": "a", "input": "test"}
    )
    assert response.status_code == 503
    assert not service.adapter.events


async def test_large_numeric_speed_and_invalid_unicode_are_400(client):
    data = {"model": "gpt-sovits", "voice": "a", "input": "test", "speed": 10**1000}
    response = await client.post("/v1/audio/speech", json=data)
    assert response.status_code == 400 and response.json()["error"]["param"] == "speed"
    response = await client.post(
        "/v1/audio/speech",
        content=b'{"model":"gpt-sovits","voice":"a","input":"\\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400 and response.json()["error"]["param"] == "input"


async def test_auth_scheme_is_case_insensitive(client, service):
    response = await client.get(
        "/v1/models", headers={"Authorization": "bearer " + service.tokens[0].decode("ascii")}
    )
    assert response.status_code == 200
