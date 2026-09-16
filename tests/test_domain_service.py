import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, AsyncMock

from backend.functions.normalization import normalize_payload
from backend.models import Domain, DomainCertificate
from backend.models.base import Base
from backend.models.certificates import CertificateStatus
from backend.models.deployments import DeploymentStatus
from backend.models.domains import DomainStatus
from backend.services.domain_service import DomainService
from backend.services.file_transaction import atomic_write
from backend.services.nginx import NginxOperationError
from backend.utils.engine import create_database
from tests.support import temporary_settings, FakeNginx


class DomainServiceTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.directory, self.settings = temporary_settings()
    self.engine, self.factory = create_database(self.settings.DB_URL)
    async with self.engine.begin() as connection:
      await connection.run_sync(Base.metadata.create_all)
    self.session = self.factory()
    self.nginx = FakeNginx()
    self.service = DomainService(settings=self.settings, nginx=self.nginx)
    self.payload = dict(type='hostname', name='example.com', upstream_host='127.0.0.1', upstream_port=3000)
    self.row = await self.service.create_domain(self.session, self.payload)
    self.domain_id = self.row['id']

  async def asyncTearDown(self):
    await self.session.close()
    await self.engine.dispose()
    self.directory.cleanup()

  async def domain(self):
    return await Domain.first(self.session, id=self.domain_id)

  def enabled_text(self, name='example.com'):
    return (Path(self.settings.NGINX_SITES_ENABLED) / f'{name}.conf').read_text()

  async def enable_tls(self):
    domain = await self.domain()
    domain.certificates.append(DomainCertificate(
      provider='certbot', cert_name='example.com', server_names=['example.com'],
      status=CertificateStatus.ACTIVE))
    domain.deployments.append(self.service.build_deployment(normalize_payload(self.payload), ssl_enabled=True))
    await self.session.commit()
    await self.service.apply_domain(self.session, self.domain_id)

  async def test_latest_deployment_with_identical_timestamps(self):
    await self.service.update_domain(self.session, dict(self.payload, id=self.domain_id, upstream_port=4000))
    domain = await self.domain()
    for deployment in domain.deployments:
      deployment.created = datetime(2026, 1, 1)
    await self.session.commit()
    await self.service.apply_domain(self.session, self.domain_id)
    self.assertIn(':4000;', self.enabled_text())
    self.assertEqual(domain.json['latest_deployment']['id'], max(a.id for a in domain.deployments))

  async def test_update_preserves_tls(self):
    await self.enable_tls()
    row = await self.service.update_domain(self.session, dict(id=self.domain_id, upstream_port=4000))
    self.assertTrue(row['latest_deployment']['ssl_enabled'])
    self.assertIn('listen 443 ssl;', row['latest_deployment']['config_text'])
    await self.service.apply_domain(self.session, self.domain_id)
    self.assertTrue((await self.domain()).json['ssl_enabled'])

  async def test_alias_requires_certificate_coverage(self):
    await self.enable_tls()
    await self.service.update_domain(self.session, dict(id=self.domain_id, server_names=['example.com', 'www.example.com']))
    with self.assertRaisesRegex(ValueError, 'certificate_does_not_cover'):
      await self.service.apply_domain(self.session, self.domain_id)
    self.assertIn('listen 443 ssl;', self.enabled_text())

  async def test_failed_test_restores_files_and_active_state(self):
    await self.service.apply_domain(self.session, self.domain_id)
    before = self.enabled_text()
    await self.service.update_domain(self.session, dict(id=self.domain_id, upstream_port=4000))
    self.nginx.test_results = [False, True]
    with self.assertRaises(NginxOperationError):
      await self.service.apply_domain(self.session, self.domain_id)
    domain = await self.domain()
    self.assertEqual(self.enabled_text(), before)
    self.assertTrue(domain.enabled)
    self.assertEqual(domain.status, DomainStatus.ACTIVE)
    self.assertEqual(domain.latest_deployment.status, DeploymentStatus.ERROR)
    self.assertEqual(domain.applied_deployment.status, DeploymentStatus.APPLIED)

  async def test_failed_reload_recovers_previous_config(self):
    await self.service.apply_domain(self.session, self.domain_id)
    before = self.enabled_text()
    await self.service.update_domain(self.session, dict(id=self.domain_id, upstream_port=4000))
    self.nginx.reload_results = [False, True]
    with self.assertRaises(NginxOperationError):
      await self.service.apply_domain(self.session, self.domain_id)
    self.assertEqual(self.enabled_text(), before)
    self.assertTrue((await self.domain()).enabled)

  async def test_database_commit_failure_restores_nginx(self):
    await self.service.apply_domain(self.session, self.domain_id)
    before = self.enabled_text()
    await self.service.update_domain(self.session, dict(id=self.domain_id, upstream_port=4000))
    with patch.object(self.session, 'commit', AsyncMock(side_effect=RuntimeError('database_commit_failed'))):
      with self.assertRaisesRegex(RuntimeError, 'database_commit_failed'):
        await self.service.apply_domain(self.session, self.domain_id)
    self.assertEqual(self.enabled_text(), before)
    self.assertEqual((await self.domain()).applied_deployment.id, self.row['latest_deployment']['id'])

  async def test_partial_file_write_failure_restores_both_files(self):
    await self.service.apply_domain(self.session, self.domain_id)
    before = self.enabled_text()
    def broken_write(files, deployment):
      atomic_write(Path(deployment.sites_available_path), b'BROKEN')
      raise OSError('simulated_disk_error')
    with patch('backend.services.file_transaction.FileTransaction.write_deployment', broken_write):
      with self.assertRaises(NginxOperationError):
        await self.service.apply_domain(self.session, self.domain_id)
    self.assertEqual(self.enabled_text(), before)
    self.assertEqual((Path(self.settings.NGINX_SITES_AVAILABLE) / 'example.com.conf').read_text(), before)

  async def test_disable_and_delete_restore_on_reload_failure(self):
    await self.service.apply_domain(self.session, self.domain_id)
    before = self.enabled_text()
    for operation in (self.service.disable_domain, self.service.delete):
      self.nginx.reload_results = [False, True]
      with self.assertRaises(NginxOperationError):
        await operation(self.session, self.domain_id)
      self.assertEqual(self.enabled_text(), before)
      self.assertTrue((await self.domain()).enabled)

  async def test_rename_removes_old_paths_after_apply(self):
    await self.service.apply_domain(self.session, self.domain_id)
    await self.service.update_domain(self.session, dict(id=self.domain_id, name='new.example.com'))
    await self.service.apply_domain(self.session, self.domain_id)
    self.assertFalse((Path(self.settings.NGINX_SITES_ENABLED) / 'example.com.conf').exists())
    self.assertFalse((Path(self.settings.NGINX_SITES_AVAILABLE) / 'example.com.conf').exists())
    self.assertIn(':3000;', self.enabled_text('new.example.com'))

  async def test_disable_uses_applied_paths_after_rename_draft(self):
    await self.service.apply_domain(self.session, self.domain_id)
    await self.service.update_domain(self.session, dict(id=self.domain_id, name='new.example.com'))
    await self.service.disable_domain(self.session, self.domain_id)
    self.assertFalse((Path(self.settings.NGINX_SITES_ENABLED) / 'example.com.conf').exists())
    self.assertFalse((await self.domain()).enabled)

  async def test_certificate_failure_restores_existing_https(self):
    await self.enable_tls()
    before = self.enabled_text()
    self.nginx.certificate_success = False
    with self.assertRaises(NginxOperationError):
      await self.service.issue_certificate(self.session, self.domain_id)
    self.assertIn('listen 443 ssl;', self.nginx.challenge_text)
    self.assertEqual(self.enabled_text(), before)
    self.assertTrue((await self.domain()).ssl_enabled)
    self.assertEqual((await self.domain()).certificates[-1].status, CertificateStatus.ERROR)

  async def test_certificate_success_sets_expiry_and_tls(self):
    row = await self.service.issue_certificate(self.session, self.domain_id)
    self.assertTrue(row['ssl_enabled'])
    self.assertTrue(row['certificates'][-1]['expires_at'])
    self.assertIn('listen 443 ssl;', self.enabled_text())
    self.assertNotIn('  return 301', self.nginx.challenge_text)

  async def test_server_alias_and_stream_port_conflicts(self):
    with self.assertRaisesRegex(ValueError, 'server_name_already'):
      await self.service.create_domain(self.session, dict(self.payload, name='other.example.com', server_names=['example.com']))
    proxy = dict(type='port_proxy', name='game_1', listen_port=25565, upstream_host='127.0.0.1', upstream_port=25565)
    await self.service.create_domain(self.session, proxy)
    with self.assertRaisesRegex(ValueError, 'listen_port_already'):
      await self.service.create_domain(self.session, dict(proxy, name='game2'))
    self.assertNotEqual(self.service.domain_to_filename('game_1'), self.service.domain_to_filename('game1'))

  async def test_writes_are_serialized(self):
    entered, release = asyncio.Event(), asyncio.Event()
    original_test = self.nginx.test
    async def slow_test(settings):
      entered.set()
      await release.wait()
      return await original_test(settings)
    self.nginx.test = slow_test
    async with self.factory() as first, self.factory() as second:
      apply_task = asyncio.create_task(self.service.apply_domain(first, self.domain_id))
      await entered.wait()
      update_task = asyncio.create_task(self.service.update_domain(second, dict(id=self.domain_id, upstream_port=4000)))
      await asyncio.sleep(0.02)
      self.assertFalse(update_task.done())
      release.set()
      await asyncio.gather(apply_task, update_task)
    self.assertIn(':3000;', self.enabled_text())
