"""Atomic local JSON persistence. Run one bot process per data directory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re


class Store:
    def __init__(self, root: Path):
        self.root = root
        for folder in ("snapshots", "jobs"):
            (root / folder).mkdir(parents=True, exist_ok=True)

    def path(self, folder: str, key: str) -> Path:
        if folder not in {"snapshots", "jobs"} or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", key):
            raise ValueError("Invalid file identifier.")
        return self.root / folder / f"{key}.json"

    def read(self, folder: str, key: str) -> dict:
        with self.path(folder, key).open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Saved JSON must contain an object.")
        return data

    def write(self, folder: str, key: str, data: dict) -> None:
        target = self.path(folder, key)
        temporary = target.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            # Persist the rename too, so a power loss cannot discard a checkpoint.
            descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def list_snapshots(self):
        for path in sorted((self.root / "snapshots").glob("*.json")):
            try:
                snapshot = self.read("snapshots", path.stem)
                if snapshot.get("id") == path.stem:
                    yield snapshot
            except (OSError, ValueError):
                # One damaged file must not hide the remaining snapshots.
                continue

    def delete(self, key: str) -> None:
        self.path("snapshots", key).unlink()


def acquire_process_lock(root: Path):
    """OS releases this lock on crashes; no stale lock-file deletion needed."""
    stream = (root / ".bot.lock").open("a+b")
    if os.name == "nt":
        import msvcrt
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            raise RuntimeError("Another bot process is using this data directory.") from None
    else:
        import fcntl
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError("Another bot process is using this data directory.") from None
    return stream
