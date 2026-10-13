"""Versioned, transactional run ledger and content-addressed stage cache."""
import contextlib
import ctypes
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = 1
log = logging.getLogger(__name__)


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


def unique_dir(root, prefix='run'):
    path = Path(root) / (prefix + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    path.mkdir(parents=True)
    return path


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'run.sqlite', timeout=30)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, SCHEMA):
            raise RuntimeError(f'Unsupported database schema {version}')
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL,
            hash TEXT, status TEXT NOT NULL, error TEXT, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS stages(source TEXT, stage TEXT, cache_key TEXT,
            status TEXT NOT NULL, attempts INTEGER NOT NULL, error TEXT, data TEXT,
            PRIMARY KEY(source,stage,cache_key));
          CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, source TEXT NOT NULL,
            data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS overrides(id TEXT PRIMARY KEY, source_hash TEXT NOT NULL,
            data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS alignments(a TEXT,b TEXT,status TEXT,data TEXT,
            PRIMARY KEY(a,b));
          PRAGMA user_version=1;
        ''')

    def close(self):
        self.db.close()

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def source(self, sid, path, digest, status, data, error=None):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?)',
                            (sid, str(path), digest, status, error, json.dumps(data)))

    def sources(self):
        return [dict(r, data=json.loads(r['data'])) for r in self.db.execute('SELECT * FROM sources ORDER BY path')]

    def stage(self, sid, stage, key, fn):
        row = self.db.execute('SELECT * FROM stages WHERE source=? AND stage=? AND cache_key=?',
                              (sid, stage, key)).fetchone()
        if row and row['status'] == 'complete':
            log.info('Cached %s',stage)
            return json.loads(row['data'])
        attempts = row['attempts'] if row else 0
        for retry in range(3):
            attempts += 1
            started=time.monotonic()
            log.info('Starting %s (attempt %d/3)',stage,retry+1)
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO stages VALUES (?,?,?,?,?,?,?)',
                                (sid, stage, key, 'running', attempts, None, None))
            try:
                data = fn()
                encoded = json.dumps(data, allow_nan=False)
            except Exception as exc:
                log.warning('%s failed after %.1fs: %s',stage,time.monotonic()-started,exc)
                with self.db:
                    self.db.execute('UPDATE stages SET status=?,error=? WHERE source=? AND stage=? AND cache_key=?',
                                    ('failed', str(exc), sid, stage, key))
                if retry == 2:
                    raise
                time.sleep(min(retry + 1, 2))
            else:
                log.info('Completed %s in %.1fs',stage,time.monotonic()-started)
                with self.db:
                    self.db.execute('UPDATE stages SET status=?,data=?,error=NULL WHERE source=? AND stage=? AND cache_key=?',
                                    ('complete', encoded, sid, stage, key))
                return data

    def replace_candidates(self, sid, items):
        with self.db:
            self.db.execute('DELETE FROM candidates WHERE source=?', (sid,))
            for item in items:
                self.db.execute('INSERT INTO candidates VALUES (?,?,?)', (item['id'], sid, json.dumps(item)))

    def candidates(self, overrides=True):
        items = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM candidates ORDER BY source,id')]
        if not overrides:
            return items
        hashes = {r['id']: r['hash'] for r in self.db.execute('SELECT id,hash FROM sources')}
        decisions = [json.loads(r['data']) for r in self.db.execute('SELECT * FROM overrides ORDER BY id')
                     if hashes.get(json.loads(r['data']).get('source')) == r['source_hash']]
        # Candidate IDs follow model wording/bounds, so re-analysis renames events. A review
        # decision owns its content interval: any candidate for the same event yields to it.
        kept = [c for c in items if not any(covers(d, c) for d in decisions)]
        return sorted(kept + decisions, key=lambda c: (c['source'], c['id']))

    def override(self, item, value):
        # Store a snapshot too: an event removed by a new judge can still be restored.
        # The anchor is the interval first reviewed; later edits never move it.
        snapshot = dict(item, **value, reviewed=True)
        snapshot.setdefault('anchor_start', item['start'])
        snapshot.setdefault('anchor_end', item['end'])
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO overrides VALUES (?,?,?)',
                            (item['id'], item['source_hash'], json.dumps(snapshot)))


def covers(decision, item):
    """Whether a reviewed decision already speaks for a (possibly renamed) candidate."""
    if decision['source'] != item['source'] or decision['source_hash'] != item['source_hash']:
        return False
    if decision['id'] == item['id']:
        return True
    a, b = min(decision['anchor_start'], decision['start']), max(decision['anchor_end'], decision['end'])
    overlap = min(b, item['end']) - max(a, item['start'])
    payoff = item.get('payoff_time')
    return (payoff is not None and a <= payoff <= b) or overlap >= .5 * (item['end'] - item['start'])


@contextlib.contextmanager
def awake():
    if os.name == 'nt':
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    try:
        yield
    finally:
        if os.name == 'nt':
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


@contextlib.contextmanager
def run_lock(directory):
    """OS advisory lock releases on crashes, unlike a stale pid sentinel."""
    path = Path(directory) / '.lock'
    with path.open('a+b') as stream:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b'0'); stream.flush(); stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError('Run is already open for writing; close review/other process.') from exc
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
