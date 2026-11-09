import json
import secrets

import httpx
import pytest

from gsv_gateway import cli, runtime
from gsv_gateway.api import create_app
from gsv_gateway.backends.gsv import GSVAdapter
from gsv_gateway.doctor import diagnose
from gsv_gateway.service import GatewayService
from tests.conftest import FakeAdapter


def token_entry(id, enabled=True):
    return {"id": id, "token": secrets.token_urlsafe(32), "enabled": enabled}


def write_entries(config, *entries):
    config.token_path.write_text("// Private credentials\n" + json.dumps({"tokens": entries}), encoding="utf-8")


async def test_independent_tokens_and_startup_snapshot(config):
    desktop = token_entry("desktop")
    test_client = token_entry("test")
    write_entries(config, desktop, test_client)
    original = GatewayService(config, FakeAdapter(config), tokens=config.read_tokens())

    async def statuses(service):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
        ) as client:
            return [
                (await client.get("/v1/models", headers={"Authorization": "Bearer " + entry["token"]})).status_code
                for entry in (desktop, test_client)
            ]

    assert await statuses(original) == [200, 200]
    write_entries(config, desktop, test_client | {"enabled": False})
    assert await statuses(original) == [200, 200]
    restarted = GatewayService(config, FakeAdapter(config), tokens=config.read_tokens())
    assert await statuses(restarted) == [200, 401]


@pytest.mark.parametrize(
    "updates,expected",
    [
        ({"id": "first"}, "Duplicate token ID"),
        ({"duplicate_value": True}, "Duplicate token ID or value"),
        ({"enabled": "false"}, "Invalid token entry #2"),
        ({"token": "非" * 43}, "Invalid token entry #2"),
    ],
)
def test_rejects_invalid_token_entries(config, updates, expected):
    first = token_entry("first")
    second = token_entry("second", enabled=False)
    if "duplicate_value" in updates:
        updates = {"token": first["token"]}
    write_entries(config, first, second | updates)
    with pytest.raises(ValueError, match=expected) as error:
        config.read_tokens()
    assert first["token"] not in str(error.value)


async def test_doctor_reports_no_enabled_tokens_without_values(config, monkeypatch):
    async def probe(_):
        return "unreachable"

    monkeypatch.setattr(GSVAdapter, "probe", probe)
    entry = token_entry("disabled", enabled=False)
    write_entries(config, entry)
    report = await diagnose(config.path, full=False)
    row = next(row for row in report["checks"] if row["code"] == "TOKEN")
    assert row["status"] == "ERROR" and "At least one token" in row["message"]
    assert entry["token"] not in json.dumps(report)


async def test_runtime_rejects_malformed_credentials_before_gateway_creation(config, monkeypatch):
    config.token_path.write_text('{"tokens":[', encoding="utf-8")
    monkeypatch.setattr(runtime, "GatewayService", lambda *args, **kwargs: pytest.fail("Created gateway"))
    with pytest.raises(ValueError, match="invalid JSONC"):
        await runtime.run(config)


def test_restart_with_legacy_token_file_keeps_running_instance(config, monkeypatch):
    monkeypatch.setattr(cli, "_state_from_path", lambda _: {"overrides": {}, "runtime_root": str(config.runtime_root)})
    monkeypatch.setattr(cli, "_stop", lambda *args: pytest.fail("Stopped gateway"))
    monkeypatch.setattr(cli, "_start", lambda *args: pytest.fail("Started gateway"))
    result = cli.main(["restart", "--config", str(config.path), "--token-file", "secrets/legacy.txt"])
    assert result == 2
