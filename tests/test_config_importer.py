import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.models import Domain
from backend.models.base import Base
from backend.services.config_importer import NginxConfigImporter


class Settings:
  def __init__(self, root: Path) -> None:
    self.NGINX_SITES_AVAILABLE = str(root / 'sites-available')
    self.NGINX_SITES_ENABLED = str(root / 'sites-enabled')
    self.NGINX_STREAMS_AVAILABLE = str(root / 'streams-available')
    self.NGINX_STREAMS_ENABLED = str(root / 'streams-enabled')
    self.LETSENCRYPT_DIR = '/etc/letsencrypt'


def test_parses_certbot_http_config(tmp_path):
  settings = Settings(tmp_path)
  enabled = Path(settings.NGINX_SITES_ENABLED)
  enabled.mkdir()
  (enabled / 'nginx-admin.conf').write_text(
    '''
server {
    server_name registry.voidspace.ru r.voidspace.ru;
    location / {
        proxy_pass http://127.0.0.1:8081;
    }
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/registry.voidspace.ru/fullchain.pem;
}
server {
    listen 80;
    server_name registry.voidspace.ru r.voidspace.ru;
    return 404;
}
''',
    encoding='utf-8',
  )

  configs = NginxConfigImporter(settings).parse_http_configs()

  assert len(configs) == 1
  config = configs[0]
  assert config.payload['domain']['name'] == 'registry.voidspace.ru'
  assert config.payload['route']['upstream_host'] == '127.0.0.1'
  assert config.payload['route']['upstream_port'] == 8081
  assert [a['name'] for a in config.payload['server_names']] == ['registry.voidspace.ru', 'r.voidspace.ru']
  assert config.ssl_enabled is True
  assert config.cert_name == 'registry.voidspace.ru'


def test_parses_stream_config(tmp_path):
  settings = Settings(tmp_path)
  enabled = Path(settings.NGINX_STREAMS_ENABLED)
  enabled.mkdir()
  (enabled / 'pgdirect.conf').write_text(
    '''
server {
    listen 5432;
    proxy_pass 10.252.1.3:5532;
    proxy_connect_timeout 10s;
    proxy_timeout 1h;
}
''',
    encoding='utf-8',
  )

  configs = NginxConfigImporter(settings).parse_stream_configs()

  assert len(configs) == 1
  config = configs[0]
  assert config.payload['domain']['name'] == 'pgdirect'
  assert config.payload['domain']['type'].value == 'port_proxy'
  assert config.payload['route']['listen_port'] == 5432
  assert config.payload['route']['upstream_host'] == '10.252.1.3'
  assert config.payload['route']['upstream_port'] == 5532


def test_import_is_idempotent(tmp_path):
  asyncio.run(_assert_import_is_idempotent(tmp_path))


async def _assert_import_is_idempotent(tmp_path):
  settings = Settings(tmp_path)
  enabled = Path(settings.NGINX_STREAMS_ENABLED)
  enabled.mkdir()
  (enabled / 'pgdirect.conf').write_text(
    '''
server {
    listen 5432;
    proxy_pass 10.252.1.3:5532;
    proxy_connect_timeout 10s;
    proxy_timeout 1h;
}
''',
    encoding='utf-8',
  )
  engine = create_async_engine('sqlite+aiosqlite:///:memory:')
  async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)

  session_maker = async_sessionmaker(engine, expire_on_commit=False)
  async with session_maker() as session:
    importer = NginxConfigImporter(settings)
    assert await importer.import_current_configs(session) == dict(imported=1, updated=0, skipped=0, errors=0)
    assert await importer.import_current_configs(session) == dict(imported=0, updated=0, skipped=1, errors=0)

    domain = await Domain.first(session, name='pgdirect')
    assert domain is not None
    assert domain.enabled is True
    assert len(domain.deployments) == 1

  await engine.dispose()


import unittest
import tempfile


class ImporterTests(unittest.TestCase):
  def run_case(self, function):
    with tempfile.TemporaryDirectory() as root:
      function(Path(root))

  def test_http(self):
    self.run_case(test_parses_certbot_http_config)

  def test_stream(self):
    self.run_case(test_parses_stream_config)

  def test_idempotent(self):
    self.run_case(test_import_is_idempotent)
