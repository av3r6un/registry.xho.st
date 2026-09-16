import unittest
from pathlib import Path
from unittest.mock import patch
from backend.models import Domain
from backend.models.base import Base
from backend.services.config_importer import NginxConfigImporter
from backend.services.domain_service import DomainService
from backend.utils.engine import create_database
from tests.support import temporary_settings, FakeNginx


class ImportSafetyTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.directory, self.settings = temporary_settings()
    self.engine, self.factory = create_database(self.settings.DB_URL)
    async with self.engine.begin() as connection:
      await connection.run_sync(Base.metadata.create_all)
    self.session = self.factory()
    self.service = DomainService(settings=self.settings, nginx=FakeNginx())

  async def asyncTearDown(self):
    await self.session.close()
    await self.engine.dispose()
    self.directory.cleanup()

  async def test_restart_preserves_draft_route_and_history(self):
    row = await self.service.create_domain(self.session, dict(name='example.com', upstream_host='127.0.0.1', upstream_port=3000))
    await self.service.apply_domain(self.session, row['id'])
    await self.service.update_domain(self.session, dict(id=row['id'], upstream_port=4000))
    stats = await NginxConfigImporter(self.settings).import_current_configs(self.session)
    domain = await Domain.first(self.session, id=row['id'])
    self.assertEqual(stats['skipped'], 1)
    self.assertEqual(domain.route.upstream_port, 4000)
    self.assertEqual(len(domain.deployments), 2)
    self.assertIn(':4000;', domain.latest_deployment.config_text)
    self.assertIn(':3000;', domain.applied_deployment.config_text)

  async def test_multi_server_stream_file_is_skipped(self):
    enabled = Path(self.settings.NGINX_STREAMS_ENABLED)
    enabled.mkdir()
    (enabled / 'multi.conf').write_text('server { listen 25565; proxy_pass 127.0.0.1:25565; }\nserver { listen 25566; proxy_pass 127.0.0.1:25566; }')
    stats = await NginxConfigImporter(self.settings).import_current_configs(self.session)
    self.assertEqual(stats['skipped'], 1)
    self.assertEqual(stats['imported'], 0)

  async def test_one_failed_import_does_not_rollback_other_files(self):
    enabled = Path(self.settings.NGINX_STREAMS_ENABLED)
    enabled.mkdir()
    for name, port in (('first', 25565), ('broken', 25566), ('third', 25567)):
      (enabled / f'{name}.conf').write_text(f'server {{ listen {port}; proxy_pass 127.0.0.1:{port}; }}')
    importer = NginxConfigImporter(self.settings)
    original = importer.upsert_config
    async def fail_one(session, config):
      if config.filename == 'broken.conf':
        raise ValueError('simulated_import_failure')
      return await original(session, config)
    with patch.object(importer, 'upsert_config', fail_one):
      stats = await importer.import_current_configs(self.session)
    self.assertEqual(stats['imported'], 2)
    self.assertEqual(stats['errors'], 1)
    self.assertIsNotNone(await Domain.first(self.session, name='first'))
    self.assertIsNotNone(await Domain.first(self.session, name='third'))
