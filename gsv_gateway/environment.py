from .audio import select_audio_tools
from .python_probe import supported_python, verify_dependencies


def validate_environment(config):
    if (config.runtime_root / "installing.json").exists():
        raise ValueError("Previous setup is incomplete")
    if not supported_python():
        raise ValueError("This release requires CPython 3.11 or 3.12 x64")
    dependencies = verify_dependencies()
    (ffmpeg, ffprobe), (versions, _) = select_audio_tools(config, dependencies=dependencies)
    return ffmpeg, ffprobe, versions
