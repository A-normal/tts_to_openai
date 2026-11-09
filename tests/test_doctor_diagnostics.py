import json

import pytest

from gsv_gateway.doctor import diagnose, load_diagnostic_config


def test_reports_resolved_files_and_preserves_configuration(config, monkeypatch):
    before = config.path.read_bytes(), config.voices_path.read_bytes()
    monkeypatch.chdir(config.base.parent)
    loaded, details = load_diagnostic_config(config.path)
    assert loaded.path == config.path and loaded.voices_path == config.voices_path
    assert not details
    assert (config.path.read_bytes(), config.voices_path.read_bytes()) == before


def test_missing_main_file_names_the_file(tmp_path):
    path = tmp_path / "missing.jsonc"
    loaded, details = load_diagnostic_config(path)
    assert loaded is None
    assert details[0]["file"] == str(path)
    assert details[0]["document"] == "gateway"
    assert details[0]["error_type"] == "file_missing"


def test_missing_voice_file_resolves_relative_to_gateway(config):
    config.voices_path.unlink()
    loaded, details = load_diagnostic_config(config.path)
    assert loaded is None
    assert details[0]["document"] == "voices"
    assert details[0]["file"] == str(config.voices_path)
    assert details[0]["error_type"] == "file_missing"


@pytest.mark.parametrize("document", ["gateway", "voices"])
def test_jsonc_syntax_error_reports_document_line_column(config, document):
    path = config.path if document == "gateway" else config.voices_path
    path.write_text('{\n"schema_version":1,\n}', encoding="utf-8")
    loaded, details = load_diagnostic_config(config.path)
    assert loaded is None
    assert details[0]["document"] == document
    assert details[0]["file"] == str(path)
    assert details[0]["line"] == 3 and details[0]["column"] == 1


def test_schema_diagnostic_has_field_but_never_input_values(config):
    data = json.loads(config.path.read_text())
    marker = "PRIVATE_INVALID_CONFIG_VALUE"
    data["server"] = {"port": marker}
    data["auth"]["token"] = marker
    config.path.write_text(json.dumps(data))
    loaded, details = load_diagnostic_config(config.path)
    assert loaded is None
    assert {d["field"] for d in details} == {"server.port", "auth.token"}
    assert marker not in json.dumps(details)
    assert all("input" not in d and "ctx" not in d for d in details)


def test_collision_names_the_config_field(config):
    data = json.loads(config.path.read_text())
    data["server"] = {"port": 9880}
    config.path.write_text(json.dumps(data))
    _, details = load_diagnostic_config(config.path)
    assert details[0]["field"] == "server.port"
    assert "9880" in details[0]["reason"]


@pytest.mark.parametrize("voice", ["missing", "角色甲"])
def test_startup_voice_is_missing_or_alias(config, voice):
    data = json.loads(config.path.read_text())
    data["startup_test"]["voice"] = voice
    config.path.write_text(json.dumps(data))
    loaded, details = load_diagnostic_config(config.path)
    assert loaded is None
    assert details[0]["field"] == "startup_test.voice"
    assert details[0]["document"] == "gateway"


def test_voice_registry_validation_reports_voice_file(config):
    voices = json.loads(config.voices_path.read_text())
    voices["voices"][1]["aliases"] = ["a"]
    config.voices_path.write_text(json.dumps(voices))
    _, details = load_diagnostic_config(config.path)
    assert details[0]["file"] == str(config.voices_path)
    assert details[0]["document"] == "voices" and details[0]["field"] == "voices"


async def test_doctor_retains_config_code_and_returns_actionable_details(config):
    config.voices_path.unlink()
    report = await diagnose(config.path, full=False)
    assert report["status"] == "APP_CONFIG_INCOMPLETE"
    row = next(row for row in report["checks"] if row["code"] == "CONFIG")
    assert row["status"] == "ERROR" and row["details"][0]["error_type"] == "file_missing"
    assert "voices_file" in row["next_action"]


@pytest.mark.parametrize(
    "source,reason", [('{"a":1,"a":2}', "重复"), ("{/* unfinished", "注释"), ('{"x":NaN}', "NaN")]
)
def test_jsonc_lexer_errors_have_safe_reasons(tmp_path, source, reason):
    path = tmp_path / "gateway.jsonc"
    path.write_text(source)
    _, details = load_diagnostic_config(path)
    assert reason in details[0]["reason"]
