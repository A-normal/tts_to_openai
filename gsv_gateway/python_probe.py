"""Probe an interpreter using only its standard library until imports are checked."""

import importlib
import importlib.metadata
import platform
import sys
import tomllib
from pathlib import Path


def supported_python():
    return (
        sys.platform == "win32"
        and sys.implementation.name == "cpython"
        and sys.version_info[:2] in {(3, 11), (3, 12)}
        and sys.maxsize > 2**32
        and platform.machine().upper() in {"AMD64", "X86_64"}
    )


def verify_dependencies(project_root=None):
    project_root = project_root or Path(__file__).resolve().parents[1]
    with (project_root / "uv.lock").open("rb") as stream:
        packages = {package["name"]: package for package in tomllib.load(stream)["package"]}
    versions = {}
    pending = ["gsv-openai-gateway"]
    while pending:
        name = pending.pop()
        if name in versions:
            continue
        package = packages[name]
        if name != "gsv-openai-gateway":
            version = importlib.metadata.version(name)
            if version != package["version"]:
                raise ValueError(f"Locked dependency version mismatch: {name}")
            versions[name] = version
        pending.extend(dep["name"] for dep in package.get("dependencies", []))
    for module in ("fastapi", "pydantic", "uvicorn", "httpx", "win32api", "win32job", "yaml"):
        importlib.import_module(module)
    return versions


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--base-only", action="store_true")
    args = parser.parse_args()
    if not supported_python():
        return 2
    import ssl
    import venv

    if not ssl.OPENSSL_VERSION or not venv.EnvBuilder:
        return 2
    ready = False
    if not args.base_only:
        try:
            verify_dependencies(args.project_root)
            ready = True
        except Exception:
            pass
    print(json.dumps({"base": str(Path(sys._base_executable).resolve()), "ready": ready}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
