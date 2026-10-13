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

from clipcompiler import settings, skill

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

    def test_a_v1_file_loads_with_the_new_defaults_and_keeps_its_values(self):
        v1 = {'version': 1, 'onboarded': True, 'source': 'D:/Clips', 'output': 'D:/Out', 'mode': 'parts',
              'judge_mode': 'batch', 'min_score': 6, 'budget_usd': 3.0, 'model': 'gemini-flash-latest'}
        (Path(self.temp.name)/'settings.json').write_text(json.dumps(v1), encoding='utf-8')
        loaded = settings.load()
        self.assertEqual((loaded.mode, loaded.judge_mode, loaded.min_score, loaded.budget_usd), ('parts', 'batch', 6, 3.))
        self.assertEqual((loaded.content_mode, loaded.min_skill_score, loaded.skill_preset, loaded.skill_share,
                          loaded.game_override), ('funny', 7, 'balanced', .4, 'auto'))   # Existing users: nothing changes.
        settings.save(loaded)
        self.assertEqual(json.loads((Path(self.temp.name)/'settings.json').read_text(encoding='utf-8'))['version'], 2)

    def test_skill_options_are_validated(self):
        for bad in ({'content_mode': 'serious'}, {'min_skill_score': 0}, {'skill_preset': 'spray'},
                    {'skill_share': .95}, {'game_override': 'valorant'}):
            with self.assertRaises(ValueError, msg=bad): settings.save(settings.Settings(**bad))
        good = settings.Settings(content_mode='both', min_skill_score=8, skill_preset='clutch', skill_share=.5, game_override='cs2')
        settings.save(good); self.assertEqual(settings.load(), good)

    def test_profiles_follow_the_skill_settings(self):
        p = settings.profiles(settings.Settings(skill_preset='aim', game_override='rust'))
        self.assertEqual((p.for_game('cs2')['name'], p.for_game('cs2')['weights']), ('rust', skill.PRESET_WEIGHTS['aim']))
        self.assertEqual(p.user_dir, Path(self.temp.name)/'skill_profiles')

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
        # A local bug is not "check your internet": it says what went wrong.
        with patch('clipcompiler.gemini.client', return_value=Client(RuntimeError('something broke'))):
            result = settings.validate_key(FAKE_KEY)
        self.assertEqual((result['state'], result['message']), ('error', 'something broke'))

    def test_validate_key_keeps_the_client_alive_for_the_call(self):
        class ClosingClient:
            """Like google-genai: a Client that is garbage-collected closes its connection."""
            def __init__(self): self.models = Models(self.state)
            state = None
            def __del__(self): self.models.closed[0] = True
        class Models:
            def __init__(self, _): self.closed = [False]
            def get(self, model):
                if self.closed[0]: raise RuntimeError('Cannot send a request, as the client has been closed.')
        with patch('clipcompiler.gemini.client', side_effect=lambda key: ClosingClient()):
            self.assertEqual(settings.validate_key(FAKE_KEY)['state'], 'ok')


if __name__ == '__main__':
    unittest.main()
