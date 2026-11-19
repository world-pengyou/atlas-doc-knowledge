import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict

from scripts import manage_database


class DatabaseSetupTests(unittest.TestCase):
    def test_url_password_characters_are_encoded_without_changing_password(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            (root / '.env').write_text(
                "DATABASE_URL=postgresql://example:synthetic@value@127.0.0.1:5432/example_db\n",
                encoding='utf-8',
            )
            with patch.object(manage_database, 'ROOT', root), patch('builtins.print'):
                info = manage_database.normalized_connection()
            self.assertEqual(info['host'], '127.0.0.1')
            self.assertEqual(info['password'], 'synthetic@value')
            persisted = dotenv_values(root / '.env')['DATABASE_URL']
            self.assertIn('%40', persisted)
            self.assertEqual(conninfo_to_dict(persisted)['dbname'], 'example_db')

    def test_database_setup_refuses_remote_hosts(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary)
            (root / '.env').write_text(
                'DATABASE_URL=postgresql://example:synthetic@remote.invalid/example_db\n',
                encoding='utf-8',
            )
            with patch.object(manage_database, 'ROOT', root):
                with self.assertRaisesRegex(ValueError, '本机'):
                    manage_database.normalized_connection()


if __name__ == '__main__':
    unittest.main()
