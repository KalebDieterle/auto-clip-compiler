"""Content hashes, atomic JSON, the hash index, and keep-awake."""
import contextlib
import ctypes
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    before = path.stat()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f'Source changed while hashing: {path}')
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temp, path)


class HashIndex:
    """Remembers each file's content hash by (path, size, mtime), so a rescan of an unchanged library
    is instant. Any change to size or mtime re-hashes; a missing or corrupt index just re-hashes."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock(); self.dirty = 0
        try:
            self.entries = json.loads(self.path.read_text(encoding='utf-8')) if self.path and self.path.exists() else {}
        except (OSError, ValueError):
            self.entries = {}

    def __call__(self, path):
        path = Path(path); stat = path.stat(); key = str(path.resolve())
        with self.lock:
            known = self.entries.get(key)
        if known and known[:2] == [stat.st_size, stat.st_mtime_ns]:
            return known[2]
        digest = file_hash(path)
        with self.lock:
            self.entries[key] = [stat.st_size, stat.st_mtime_ns, digest]; self.dirty += 1
            if self.dirty >= 50: self._save()
        return digest

    def save(self):
        with self.lock: self._save()

    def _save(self):
        if self.path and self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.path, self.entries)
        self.dirty = 0


@contextlib.contextmanager
def awake():
    if os.name == 'nt':
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    try:
        yield
    finally:
        if os.name == 'nt':
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
