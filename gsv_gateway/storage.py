import json
import os
import tempfile

import yaml

from .security import require_private, secure_path


def atomic_write(path, data):
    secure_path(path.parent, directory=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        from pathlib import Path

        secure_path(Path(name))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class SnapshotStore:
    def __init__(self, config, adapter):
        self.config, self.adapter = config, adapter
        self.current = None

    def candidate(self, voice):
        data = self.adapter.snapshot(voice)
        if (
            data.get("voice") != voice.id
            or data.get("backend_type") != self.config.value.backend.type
            or data.get("registry_sha256") != self.config.registry_digest
        ):
            raise ValueError("Snapshot must identify the current backend, registry and voice")
        return data

    def select(self):
        path = self.config.snapshot_path
        if not path.exists():
            return self.config.registry.find(self.config.value.startup_test.voice), "startup_voice"
        require_private(path)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Snapshot too large")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            voice = self.config.registry.find(data["voice"])
            expected = self.candidate(voice)
            if data != expected:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                "Last-good snapshot is stale or invalid; repair or explicitly remove it"
            ) from None
        self.current = expected
        return voice, "last_good"

    def commit(self, voice):
        candidate = self.candidate(voice)
        if candidate != self.current:
            atomic_write(
                self.config.snapshot_path, yaml.safe_dump(candidate, allow_unicode=True).encode("utf-8")
            )
            self.current = candidate


def write_state(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False).encode())
