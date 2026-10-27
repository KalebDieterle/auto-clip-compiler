"""User settings (plain JSON) and the Gemini API key (Windows Credential Manager, never a file).

The key is only ever read from keyring when a job starts; the UI sees a masked form, and every log
record passes through a filter that redacts it.
"""
from dataclasses import asdict, dataclass, fields
import json
import logging
from logging.handlers import RotatingFileHandler
import re

from . import paths
from .gemini import DEFAULT_MODEL
from .store import atomic_json

SCHEMA_VERSION = 2   # 2: compilation type (funny / skill / both) and skill options.
KEY_SERVICE, KEY_USER = 'AutoClipCompiler', 'gemini-api-key'
KEY_SHAPE = re.compile(r'AIza[0-9A-Za-z_\-]{30,}')


@dataclass
class Settings:
    version: int = SCHEMA_VERSION
    onboarded: bool = False
    source: str = ''
    output: str = ''
    cache_dir: str = ''              # Empty: the app's own cache folder.
    mode: str = 'reel'               # 'reel' = one video of target_minutes; 'parts' = whole range split into parts.
    judge_mode: str = 'live'         # 'live' = results in minutes; 'batch' = Batch API, half price, up to 24 h.
    target_minutes: float = 30.
    part_minutes: float = 30.
    min_score: int = 7
    budget_usd: float = 5.           # 0 = no cap.
    workers: int = 4
    height: int = 1080
    fps: int = 60
    loudnorm: bool = True
    timestamp_overlay: bool = False
    model: str = DEFAULT_MODEL
    content_mode: str = 'funny'      # 'funny', 'skill' or 'both' (one mixed reel, each moment tagged).
    min_skill_score: int = 7
    skill_preset: str = 'balanced'   # 'balanced', 'aim' or 'clutch'.
    skill_share: float = .4          # Both mode: target fraction of the reel that is skill plays.
    game_override: str = 'auto'      # 'auto' (detected per clip), 'cs2', 'rust' or 'generic'.
    lookback: bool = True            # After judging, look back before each kept funny moment for its setup.
    skip_shown: bool = False         # Leave out moments a rendered compilation already played.

    def problems(self):
        checks = [(self.mode in ('reel', 'parts'), 'mode must be reel or parts'),
                  (self.judge_mode in ('live', 'batch'), 'judging must be live or batch'),
                  (1 <= self.min_score <= 10, 'minimum score must be 1-10'),
                  (1 <= self.target_minutes <= 600, 'reel length must be 1-600 minutes'),
                  (1 <= self.part_minutes <= 600, 'part length must be 1-600 minutes'),
                  (self.budget_usd >= 0, 'budget cannot be negative'),
                  (1 <= self.workers <= 8, 'workers must be 1-8'),
                  (self.height in (720, 1080, 1440), 'quality must be 720, 1080 or 1440'),
                  (self.fps in (30, 60), 'frame rate must be 30 or 60'),
                  (self.content_mode in ('funny', 'skill', 'both'), 'compilation type must be funny, skill or both'),
                  (1 <= self.min_skill_score <= 10, 'minimum skill score must be 1-10'),
                  (self.skill_preset in ('balanced', 'aim', 'clutch'), 'skill focus must be balanced, aim or clutch'),
                  (.1 <= self.skill_share <= .9, 'skill share must be 10-90%'),
                  (self.game_override in ('auto', 'cs2', 'rust', 'generic'), 'game must be auto, cs2, rust or generic')]
        return [message for ok, message in checks if not ok]


def load(path=None):
    """Settings from disk; anything missing, unknown, mistyped or corrupt falls back to the default."""
    path = path or paths.settings_path(); result = Settings()
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return result
    if not isinstance(raw, dict): return result
    for f in fields(Settings):
        if f.name == 'version' or f.name not in raw: continue
        value, default = raw[f.name], getattr(result, f.name)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, (int, float)):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            value = type(default)(value) if ok else value
        else:
            ok = isinstance(value, type(default))
        if ok: setattr(result, f.name, value)
    if result.problems():
        return Settings(**{k: v for k, v in asdict(result).items()
                           if k in ('onboarded', 'source', 'output', 'cache_dir')})
    return result


def save(settings, path=None):
    problems = settings.problems()
    if problems: raise ValueError('; '.join(problems))
    path = path or paths.settings_path(); path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, asdict(settings))


def profiles_dir():
    """User copies of the skill profiles (optional); a file here overrides the built-in one key by key."""
    return paths.config_dir()/'skill_profiles'


def profiles(settings):
    from .skill import Profiles
    return Profiles(settings.skill_preset, settings.game_override, profiles_dir())


def cache_dir(settings=None):
    from pathlib import Path
    settings = settings or load()
    return Path(settings.cache_dir) if settings.cache_dir else paths.cache_dir()


# --- API key -------------------------------------------------------------------------------------

def get_key():
    import keyring
    key = keyring.get_password(KEY_SERVICE, KEY_USER)
    if key: _known_keys.add(key)
    return key


def set_key(key):
    import keyring
    key = (key or '').strip()
    if not key or len(key) < 20 or any(c.isspace() for c in key):
        raise ValueError("That doesn't look like a Gemini API key.")
    _known_keys.add(key)
    keyring.set_password(KEY_SERVICE, KEY_USER, key)


def clear_key():
    import keyring
    try: keyring.delete_password(KEY_SERVICE, KEY_USER)
    except keyring.errors.PasswordDeleteError: pass


def masked(key):
    return f'••••{key[-4:]}' if key else ''


def validate_key(key, model=DEFAULT_MODEL):
    """A free call (model metadata) that proves the key works. Returns {'ok', 'state', 'message'}."""
    from .gemini import client
    _known_keys.add(key)
    try:
        gc = client(key)  # Keep a reference: a garbage-collected Client closes its connection mid-call.
        gc.models.get(model=model)
        return {'ok': True, 'state': 'ok', 'message': 'Key works.'}
    except Exception as exc:
        text = redact(str(exc)); code = getattr(exc, 'code', None)
        if code in (400, 401, 403) or 'api key' in text.lower():
            return {'ok': False, 'state': 'invalid', 'message': 'Google rejected this key. Check it was copied in full.'}
        if code is None and is_network_error(exc):
            return {'ok': False, 'state': 'network', 'message': "Couldn't reach Google. Check your internet connection."}
        logging.getLogger(__name__).warning('key check failed: %s: %s', type(exc).__name__, text)
        return {'ok': False, 'state': 'error', 'message': text[:300] or type(exc).__name__}


def is_network_error(exc):
    """Connection, DNS, TLS and timeout failures (httpx or stdlib), anywhere in the exception chain."""
    while exc is not None:
        name = type(exc).__name__
        if isinstance(exc, OSError) or name in (
                'ConnectError', 'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout', 'NetworkError',
                'TimeoutException', 'RemoteProtocolError', 'SSLError', 'gaierror'):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


# --- Logging -------------------------------------------------------------------------------------

_known_keys = set()  # Keys seen this session, redacted even if Google changes the key format.


def redact(text):
    for key in _known_keys: text = text.replace(key, '[redacted key]')
    return KEY_SHAPE.sub('[redacted key]', text)


class RedactingFormatter(logging.Formatter):
    """Redacts the formatted line, tracebacks included."""
    def format(self, record):
        return redact(super().format(record))


def setup_logging(level=logging.INFO):
    directory = paths.logs_dir(); directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory/'app.log', maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(RedactingFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    root = logging.getLogger(); root.setLevel(level); root.addHandler(handler)
    return directory/'app.log'
