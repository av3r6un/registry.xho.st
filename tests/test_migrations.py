import os
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]


class MigrationTests(unittest.TestCase):
  def test_existing_schema_tls_backfill_and_round_trip(self):
    with tempfile.TemporaryDirectory() as directory:
      database = Path(directory) / 'test.db'
      config = Config(str(ROOT / 'alembic.ini'))
      config.set_main_option('script_location', str(ROOT / 'alembic'))
      with patch.dict(os.environ, {'DB_URL': f'sqlite+aiosqlite:///{database.as_posix()}'}):
        command.upgrade(config, '4756f3345db0')
        with closing(sqlite3.connect(database)) as connection:
          connection.execute("INSERT INTO domains (id,name,type,enabled,status,created,updated) VALUES (1,'example.com','HOSTNAME',1,'ACTIVE','2026-01-01','2026-01-01')")
          for value in (1, 2):
            connection.execute("INSERT INTO domain_deployments (id,domain_id,status,nginx_filename,sites_available_path,sites_enabled_path,config_text,created,updated) VALUES (?,1,'APPLIED','example.com.conf','/a/example.com.conf','/e/example.com.conf',?,'2026-01-01','2026-01-01')",
                       (value, 'server { listen 443 ssl; ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem; }'))
          connection.commit()
        command.upgrade(config, 'head')
        command.check(config)
        with closing(sqlite3.connect(database)) as connection:
          rows = connection.execute('SELECT status, ssl_enabled, cert_name FROM domain_deployments ORDER BY id').fetchall()
          self.assertEqual(rows, [('SUPERSEDED', 1, 'example.com'), ('APPLIED', 1, 'example.com')])
        command.downgrade(config, '4756f3345db0')
        command.upgrade(config, 'head')
        command.check(config)
