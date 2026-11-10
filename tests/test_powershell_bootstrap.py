import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from gsv_gateway.config import TokenFile
from gsv_gateway.security import require_private
from gsv_gateway.windows import open_runtime_lease


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1")
def test_doctor_without_prepared_or_global_python(config, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("common.ps1", "doctor.ps1"):
        shutil.copyfile(source / name, scripts / name)
    env = os.environ.copy()
    env["PATH"] = str(Path(env["SYSTEMROOT"]) / "System32")
    executable = Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [
            str(executable),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / "doctor.ps1"),
            "-Config",
            str(config.path),
            "-Json",
        ],
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 2
    report = json.loads(result.stdout.decode("utf-8-sig"))
    assert report["status"] == "ENV_NOT_READY"
    codes = {row["code"] for row in report["checks"]}
    assert {"CONFIG_JSONC", "PYTHON_NOT_PREPARED", "DEEP_CHECKS"} <= codes
    assert not (tmp_path / "runtime").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell and private ACLs")
def test_setup_token_initialization_is_private_and_idempotent(tmp_path):
    source = Path(__file__).resolve().parents[1] / "scripts" / "common.ps1"
    directory = tmp_path / "private tokens"
    env = os.environ | {"GATEWAY_TEST_COMMON": str(source), "GATEWAY_TEST_SECRETS": str(directory)}
    executable = Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [
        str(executable),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ". $env:GATEWAY_TEST_COMMON; Initialize-GatewayTokens $env:GATEWAY_TEST_SECRETS",
    ]
    subprocess.run(command, env=env, capture_output=True, check=True, timeout=15)
    path = directory / "api-tokens.jsonc"
    require_private(directory)
    require_private(path)
    before = path.read_bytes()
    entries = TokenFile.model_validate_json(before).tokens
    assert len(entries) == 1 and entries[0].enabled and len(entries[0].token) == 43
    subprocess.run(command, env=env, capture_output=True, check=True, timeout=15)
    assert path.read_bytes() == before
    assert list(directory.iterdir()) == [path]


@pytest.mark.skipif(os.name != "nt", reason="Windows shared runtime lease")
def test_powershell_setup_excludes_active_runtime_readers(tmp_path):
    source = Path(__file__).resolve().parents[1] / "scripts" / "common.ps1"
    env = os.environ | {"GATEWAY_TEST_COMMON": str(source), "GATEWAY_TEST_RUNTIME": str(tmp_path)}
    executable = Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [
        str(executable),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ". $env:GATEWAY_TEST_COMMON; $lease = Open-RuntimeRepairLease $env:GATEWAY_TEST_RUNTIME; $lease.Dispose()",
    ]
    first = open_runtime_lease(tmp_path)
    try:
        second = open_runtime_lease(tmp_path)
        try:
            blocked = subprocess.run(command, env=env, capture_output=True, timeout=15)
            assert blocked.returncode != 0 and b"Cannot lock the shared runtime" in blocked.stderr
        finally:
            second.Close()
    finally:
        first.Close()
    subprocess.run(command, env=env, capture_output=True, check=True, timeout=15)
