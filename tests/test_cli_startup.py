import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gsv_gateway import cli
from gsv_gateway.errors import GatewayError


@pytest.fixture
def startup(tmp_path, monkeypatch):
    """All launch/IPC/Job operations are substitutes; no process is started."""
    config = SimpleNamespace(
        path=tmp_path / "gateway.jsonc",
        runtime_root=tmp_path / "runtime",
        effective_digest="expected-digest",
        overrides={},
        value=SimpleNamespace(timeouts=SimpleNamespace(startup_seconds=1, shutdown_drain_seconds=1)),
    )
    nonce = "a" * 32
    state = {
        "pid": 200,
        "created": "interpreter-time",
        "nonce": nonce,
        "effective_digest": config.effective_digest,
    }
    handle, job = Mock(), Mock()
    launched = Mock(return_value=(handle, 100, "redirector-time", job))
    matcher = Mock(return_value=True)
    release, terminate, wait = Mock(), Mock(), AsyncMock()
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: SimpleNamespace(hex=nonce))
    monkeypatch.setattr(cli, "detached_gateway", launched)
    monkeypatch.setattr(cli, "_state", Mock(side_effect=[None, state]))
    monkeypatch.setattr(cli, "_status", Mock(return_value={"ready": True}))
    monkeypatch.setattr(cli, "job_has_processes", Mock(return_value=True))
    monkeypatch.setattr(cli, "startup_instance_matches", matcher)
    monkeypatch.setattr(cli, "release_startup_job", release)
    monkeypatch.setattr(cli, "wait_job_empty", wait)
    import win32job
    import win32process

    monkeypatch.setattr(win32job, "TerminateJobObject", terminate)
    monkeypatch.setattr(win32process, "GetExitCodeProcess", lambda handle: 101)
    return SimpleNamespace(**locals())


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher cleanup")


def test_start_accepts_verified_interpreter_with_different_pid(startup, capsys):
    cli._start(startup.config)
    assert capsys.readouterr().out == "ready\n"
    startup.matcher.assert_called_once_with(startup.state, startup.nonce, startup.job)
    startup.release.assert_called_once_with(startup.job)
    startup.terminate.assert_not_called()
    startup.wait.assert_not_awaited()
    startup.job.Close.assert_called_once()
    startup.handle.Close.assert_called_once()


def test_start_displays_verified_progress_and_keeps_ready_on_stdout(startup, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_state", Mock(side_effect=[None, None] + [startup.state] * 3))
    monkeypatch.setattr(
        cli,
        "_status",
        Mock(
            side_effect=[
                {"ready": False, "startup_phase": "backend_launch"},
                {"ready": False, "startup_phase": "startup_self_test"},
                {"ready": True},
            ]
        ),
    )
    cli._start(startup.config)
    output = capsys.readouterr()
    assert output.out == "ready\n"
    assert [line.split(" (")[0] for line in output.err.splitlines()] == [
        "[gateway] Launching the gateway process",
        "[gateway] Waiting for the gateway control channel",
        "[gateway] Waiting for the managed TTS backend to start",
        "[gateway] Running the FLAC synthesis and decode self-test",
        "[gateway] Gateway ready",
    ]


@pytest.mark.parametrize("failure", ["identity", "config", "exited"])
def test_start_failure_cleans_only_owned_job(startup, monkeypatch, failure):
    if failure == "identity":
        startup.matcher.return_value = False
        expected = "startup_identity_conflict"
    elif failure == "config":
        startup.state["effective_digest"] = "changed-digest"
        expected = "startup_config_changed"
    else:
        monkeypatch.setattr(cli, "job_has_processes", lambda job: False)
        expected = "startup_exited"
    with pytest.raises(GatewayError) as caught:
        cli._start(startup.config)
    assert caught.value.code == expected
    if failure == "exited":
        assert "exit_code=101" in caught.value.message
    startup.release.assert_not_called()
    startup.terminate.assert_called_once_with(startup.job, 1)
    startup.wait.assert_awaited_once_with(startup.job, 1)
    startup.job.Close.assert_called_once()
    startup.handle.Close.assert_called_once()


def test_start_failure_reports_safe_specific_reason(monkeypatch, capsys):
    monkeypatch.setattr(cli, "LoadedConfig", Mock(return_value=object()))
    monkeypatch.setattr(
        cli,
        "_start",
        Mock(
            side_effect=GatewayError(
                "startup_identity_conflict", "Startup process identity verification failed"
            )
        ),
    )
    assert cli.main(["start", "--config", "unused.jsonc"]) == 1
    assert capsys.readouterr().err == (
        "startup_identity_conflict: Startup process identity verification failed\n"
    )


def test_child_failure_is_reported_without_raw_exception(startup, monkeypatch):
    import win32process

    from gsv_gateway import runtime

    async def fail(config, nonce, *, startup):
        startup["phase"] = "api_listener"
        raise OSError(10048, "secret from exception payload", None, 10048)

    monkeypatch.setattr(cli, "LoadedConfig", Mock(return_value=startup.config))
    monkeypatch.setattr(runtime, "run", fail)
    assert cli.main(["run", "--config", str(startup.config.path), "--startup-nonce", startup.nonce]) == 1
    path = cli._startup_failure_path(startup.config.path, startup.nonce)
    assert "secret from exception payload" not in path.read_text(encoding="utf-8")
    monkeypatch.setattr(win32process, "GetExitCodeProcess", lambda handle: 1)
    monkeypatch.setattr(cli, "job_has_processes", lambda job: False)
    with pytest.raises(GatewayError) as caught:
        cli._start(startup.config)
    assert caught.value.code == "startup_exited"
    for detail in ("exit_code=1", "phase=api_listener", "reason=OSError", "winerror=10048"):
        assert detail in caught.value.message
    assert not path.exists()
