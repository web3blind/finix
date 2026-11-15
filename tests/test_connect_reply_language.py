"""Owner chat-language boundary; synthetic profiles only, no providers."""
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest

from finix.integration import command


class ConnectReplyLanguageTests(unittest.TestCase):
    def reply(self, text=None):
        # Hosted Windows TEMP may contain an 8.3 alias; use the runner's long path.
        with tempfile.TemporaryDirectory(dir=os.environ.get('RUNNER_TEMP')) as tmp:
            home = Path(tmp)
            if text is not None:
                (home / 'config.yaml').write_text(text, encoding='utf-8')
            return command('connect', owner_id='owner', owner_platform='telegram',
                           platform='telegram',
                           source=SimpleNamespace(user_id='owner', chat_type='group'),
                           bound_profile=lambda: SimpleNamespace(home=home))

    def test_missing_locale_defaults_to_english(self):
        self.assertEqual(self.reply(),
            'Finix connect: PRIVATE_CHAT_REQUIRED; connect cloud storage only in the owner\'s private chat.')

    def test_unknown_locale_does_not_inherit_russian(self):
        self.assertEqual(self.reply('display:\n  language: de\n'),
            'Finix connect: PRIVATE_CHAT_REQUIRED; connect cloud storage only in the owner\'s private chat.')

    def test_explicit_russian_remains_available(self):
        self.assertEqual(self.reply('display:\n  language: ru\n'),
            'Finix connect: PRIVATE_CHAT_REQUIRED; подключайте облако только в личном чате владельца.')

    def test_missing_display_defaults_to_english(self):
        self.assertIn('connect cloud storage only', self.reply('model: {}\n'))

    def test_invalid_yaml_defaults_to_english_without_echo(self):
        result = self.reply('display: [PRIVATE-SENTINEL\n')
        self.assertIn('connect cloud storage only', result)
        self.assertNotIn('PRIVATE-SENTINEL', result)


if __name__ == '__main__':
    unittest.main()
