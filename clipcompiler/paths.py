"""Where the app keeps its data. Override the root with ACC_HOME (tests, portable use)."""
import os
from pathlib import Path

APP = 'AutoClipCompiler'


def _root(variable):
    if os.environ.get('ACC_HOME'):
        return Path(os.environ['ACC_HOME'])
    return Path(os.environ.get(variable) or Path.home()/'AppData'/('Local' if variable == 'LOCALAPPDATA' else 'Roaming'))/APP


def data_dir():
    """Machine-local, possibly large: judgment cache, previews, hash index, logs."""
    return _root('LOCALAPPDATA')


def config_dir():
    return _root('APPDATA')


def cache_dir():
    return data_dir()/'gemini-cache'


def previews_dir():
    return data_dir()/'previews'


def logs_dir():
    return data_dir()/'logs'


def hash_index_path():
    return data_dir()/'hash-index.json'


def settings_path():
    return config_dir()/'settings.json'
