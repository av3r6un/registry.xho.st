import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.services.file_transaction import FileTransaction
from backend.services.nginx import Nginx, NginxOperationError
from tests.support import temporary_settings, FakeNginx


class NginxTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.directory, self.settings = temporary_settings()

  async def asyncTearDown(self):
    self.directory.cleanup()

  async def test_backups_are_distinct_for_equal_filenames(self):
    available = Path(self.settings.NGINX_SITES_AVAILABLE) / 'example.conf'
    enabled = Path(self.settings.NGINX_SITES_ENABLED) / 'example.conf'
    available.parent.mkdir()
    enabled.parent.mkdir()
    available.write_text('OLD_AVAILABLE')
    enabled.write_text('OLD_ENABLED')
    transaction = FileTransaction(self.settings, [available, enabled])
    transaction.capture()
    available.write_text('NEW')
    enabled.write_text('NEW')
    transaction.rollback()
    self.assertEqual(available.read_text(), 'OLD_AVAILABLE')
    self.assertEqual(enabled.read_text(), 'OLD_ENABLED')
    self.assertEqual(len(list(transaction.directory.glob('*.bak'))), 2)

  @unittest.skipIf(os.name == 'nt', 'Windows symlink creation may require elevated privileges')
  async def test_symlink_and_target_survive_rollback(self):
    source = Path(self.settings.NGINX_SITES_AVAILABLE) / 'example.conf'
    link = Path(self.settings.NGINX_SITES_ENABLED) / 'example.conf'
    source.parent.mkdir()
    link.parent.mkdir()
    source.write_text('ORIGINAL')
    link.symlink_to(source)
    transaction = FileTransaction(self.settings, [source, link])
    transaction.capture()
    source.write_text('NEW')
    link.unlink()
    link.write_text('NEW')
    transaction.rollback()
    self.assertTrue(link.is_symlink())
    self.assertEqual(source.read_text(), 'ORIGINAL')
    self.assertEqual(link.read_text(), 'ORIGINAL')

  async def test_paths_outside_config_directories_are_rejected(self):
    with self.assertRaisesRegex(ValueError, 'outside_config'):
      FileTransaction(self.settings, [Path(self.directory.name) / 'unrelated.conf'])

  async def test_subprocess_timeout_and_missing_executable(self):
    nginx = Nginx()
    result = await nginx.run_command(sys.executable, '-c', 'import time; time.sleep(10)', timeout=0.05)
    self.assertFalse(result['success'])
    self.assertEqual(result['stderr'], 'command_timeout')
    self.assertFalse((await nginx.run_command('missing-registry-command-unique'))['success'])

  async def test_staging_zero_does_not_add_flag(self):
    from backend.config import Settings
    nginx = Nginx()
    nginx.run_command = AsyncMock(return_value=dict(success=True))
    for value, expected in (('0', False), ('1', True), ('false', False)):
      settings = self.settings
      from backend.config.config import parse_bool
      settings.CERTBOT_STAGING = parse_bool(value)
      await nginx.issue_certificate('example.com', ['example.com'], settings)
      self.assertEqual('--staging' in nginx.run_command.call_args.args, expected)

  async def test_acme_redirect_is_scoped_to_location(self):
    text = Nginx().render_http_config(name='example.com', server_names=['example.com'], upstream_host='127.0.0.1', upstream_port=3000, ssl_enabled=True)
    self.assertIn('  location / {\n    return 301', text)
    self.assertNotIn('\n  return 301', text)

  async def test_ipv6_upstream_is_bracketed(self):
    text = Nginx().render_http_config(name='example.com', upstream_host='::1', upstream_port=3000)
    self.assertIn('proxy_pass http://[::1]:3000;', text)

  async def test_failed_rollback_is_reported(self):
    nginx = FakeNginx()
    nginx.test_results = [False]
    path = Path(self.settings.NGINX_SITES_ENABLED) / 'example.conf'
    with self.assertRaisesRegex(NginxOperationError, 'rollback_failed') as raised:
      async with nginx.transaction(self.settings, [path]):
        raise NginxOperationError('initial_failure')
    self.assertTrue(raised.exception.rollback_failed)
