import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


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
