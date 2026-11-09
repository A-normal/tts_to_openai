from unittest.mock import Mock

import pytest

from gsv_gateway import audio


def test_audio_skips_incompatible_path_pairs_and_uses_managed_fallback(config, monkeypatch):
    roots = [config.base / name for name in ("first", "second", "managed")]
    pairs = [tuple(root / f"{name}.exe" for name in ("ffmpeg", "ffprobe")) for root in roots]
    monkeypatch.setattr(audio, "path_audio_tools", lambda: iter(pairs[:2]))
    managed = Mock(return_value=pairs[2])
    monkeypatch.setattr(audio, "_managed_audio_tools", managed)
    good = ([], [])
    probe = Mock(side_effect=[ValueError("incompatible"), good])
    monkeypatch.setattr(audio, "check_audio_pair", probe)
    assert audio.select_audio_tools(config) == (pairs[1], good)
    managed.assert_not_called()

    probe.side_effect = [ValueError("incompatible"), ValueError("incompatible"), good]
    assert audio.select_audio_tools(config) == (pairs[2], good)
    managed.assert_called_once_with(config.runtime_root)


def test_invalid_explicit_audio_pair_does_not_try_path(config, monkeypatch):
    config.value.audio.ffmpeg_executable = "custom/ffmpeg.exe"
    search = Mock()
    monkeypatch.setattr(audio, "path_audio_tools", search)
    with pytest.raises(FileNotFoundError):
        audio.select_audio_tools(config)
    search.assert_not_called()
