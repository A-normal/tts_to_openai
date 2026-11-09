"""Explicit setup finalization; never invoked by ordinary run/start."""

import argparse
import json
import platform
import sys
from pathlib import Path

from .audio import _managed_audio_tools, check_audio_pair, select_path_audio_tools
from .config import Audio, fingerprint
from .python_probe import supported_python, verify_dependencies
from .storage import write_state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--ffprobe", type=Path)
    args = parser.parse_args()
    root = args.runtime_root.resolve()
    if not supported_python():
        raise ValueError("CPython 3.11 or 3.12 x64 required")
    packages = verify_dependencies(root.parent)
    if bool(args.ffmpeg) != bool(args.ffprobe):
        parser.error("--ffmpeg and --ffprobe must be provided together")
    if args.ffmpeg:
        pair = args.ffmpeg.resolve(), args.ffprobe.resolve()
        if any(not tool.is_relative_to(root) for tool in pair):
            raise ValueError("Tool outside managed runtime")
        versions, results = check_audio_pair(Audio(), *pair)
        selected = pair
    else:
        path_tools = select_path_audio_tools(Audio())
        if path_tools is None:
            return 2
        selected, (versions, results) = path_tools
        try:
            pair = _managed_audio_tools(root)
        except (OSError, ValueError):
            pair = None
    write_state(
        root / "installed.json",
        {
            "schema_version": 1,
            "project_root": str(root.parent),
            "python": sys.executable,
            "base_python": sys._base_executable,
            "python_version": platform.python_version(),
            "dependencies": packages,
            "lock_sha256": fingerprint(root.parent / "uv.lock"),
            "tools": dict(zip(("ffmpeg", "ffprobe"), map(str, pair), strict=True)) if pair else {},
            "checked_audio_tools": dict(zip(("ffmpeg", "ffprobe"), map(str, selected), strict=True)),
            "tool_sha256": dict(zip(("ffmpeg", "ffprobe"), map(fingerprint, selected), strict=True)),
            "tool_versions": versions,
            "codec_results": results,
        },
    )
    print(json.dumps({"status": "ENV_READY", "formats": len(results)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
