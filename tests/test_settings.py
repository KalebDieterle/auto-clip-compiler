"""Settings persistence, the keyring-held API key, and log redaction (in-memory keyring, temp app dir)."""
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import keyring
from keyring.backend import KeyringBackend

from clipcompiler import settings

FAKE_KEY = 'AIzaSyD' + 'x'*32


class MemoryKeyring(KeyringBackend):
    priority = 1
    def __init__(self): super().__init__(); self.store = {}
    def get_password(self, service, user): return self.store.get((service, user))
    def set_password(self, service, user, password): self.store[(service, user)] = password
    def delete_password(self, service, user):
        if (service, user) not in self.store: raise keyring.errors.PasswordDeleteError()
        del self.store[(service, user)]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'ACC_HOME': self.temp.name}); self.env.start()
        self.previous = keyring.get_keyring(); self.ring = MemoryKeyring(); keyring.set_keyring(self.ring)

    def tearDown(self):
        keyring.set_keyring(self.previous); self.env.stop(); self.temp.cleanup()

    def test_round_trip(self):
        s = settings.Settings(source='D:/Clips', mode='parts', min_score=6, budget_usd=2.5, onboarded=True)
        settings.save(s)
        self.assertEqual(settings.load(), s)

    def test_missing_corrupt_and_mistyped_values_fall_back_to_defaults(self):
        self.assertEqual(settings.load(), settings.Settings())
        path = Path(self.temp.name)/'settings.json'
        path.write_text('{not json', encoding='utf-8'); self.assertEqual(settings.load(), settings.Settings())
        path.write_text(json.dumps({'min_score': 'high', 'workers': 3, 'loudnorm': 1, 'future_option': True,
                                    'target_minutes': 12}), encoding='utf-8')
        loaded = settings.load()
        self.assertEqual((loaded.min_score, loaded.workers, loaded.loudnorm, loaded.target_minutes), (7, 3, True, 12.))

    def test_out_of_range_values_are_refused_on_save_and_reset_on_load(self):
        with self.assertRaises(ValueError): settings.save(settings.Settings(min_score=11))
        path = Path(self.temp.name)/'settings.json'
        path.write_text(json.dumps({'min_score': 50, 'source': 'D:/Clips', 'onboarded': True}), encoding='utf-8')
        loaded = settings.load()
        self.assertEqual((loaded.min_score, loaded.source, loaded.onboarded), (7, 'D:/Clips', True))

    def test_key_lives_in_keyring_never_in_settings(self):
        settings.set_key(f'  {FAKE_KEY}\n'); settings.save(settings.Settings(onboarded=True))
        self.assertEqual(settings.get_key(), FAKE_KEY)
        self.assertNotIn(FAKE_KEY, (Path(self.temp.name)/'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(settings.masked(FAKE_KEY), '\u2022\u2022\u2022\u2022xxxx')
        settings.clear_key(); settings.clear_key()
        self.assertIsNone(settings.get_key())

    def test_obviously_wrong_keys_are_refused(self):
        for bad in ('', 'short', 'has a space in the middle of it ok'):
            with self.assertRaises(ValueError): settings.set_key(bad)

    def test_logs_never_contain_the_key_even_in_tracebacks(self):
        log_path = settings.setup_logging()
        settings.set_key('custom-format-key-0123456789abcdef')
        log = logging.getLogger('test-redaction')
        try:
            raise RuntimeError(f'request failed for key={FAKE_KEY} and custom-format-key-0123456789abcdef')
        except RuntimeError:
            log.exception('upload failed with %s', FAKE_KEY)
        for h in logging.getLogger().handlers: h.flush()
        text = log_path.read_text(encoding='utf-8')
        self.assertIn('[redacted key]', text); self.assertIn('upload failed', text)
        self.assertNotIn(FAKE_KEY, text); self.assertNotIn('custom-format-key-0123456789abcdef', text)
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, 'baseFilename', '') == str(log_path): root.removeHandler(h); h.close()

    def test_validate_key_maps_errors_to_friendly_states(self):
        class Rejected(Exception): code = 400
        class Client:
            def __init__(self, error): self.models = self; self.error = error
            def get(self, model):
                if self.error: raise self.error
        cases = [(None, 'ok'), (Rejected('API key not valid'), 'invalid'), (OSError('getaddrinfo failed'), 'network')]
        for error, state in cases:
            with patch('clipcompiler.gemini.client', return_value=Client(error)):
                self.assertEqual(settings.validate_key(FAKE_KEY)['state'], state)


if __name__ == '__main__':
    unittest.main()
