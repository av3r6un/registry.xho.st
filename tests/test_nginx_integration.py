import asyncio
import os
import re
import shutil
import socket
import subprocess
import unittest
from pathlib import Path

from backend.models.base import Base
from backend.services.domain_service import DomainService
from backend.services.nginx import Nginx
from backend.utils.engine import create_database
from tests.support import temporary_settings


class UnprivilegedNginx(Nginx):
  def __init__(self):
    super().__init__()
    # nginx -t checks listener sockets too. Keep CI independent of root access
    # and services already using the production HTTP/HTTPS ports.
    with socket.socket() as http, socket.socket() as https:
      http.bind(('127.0.0.1', 0))
      https.bind(('127.0.0.1', 0))
      self.ports = {80: http.getsockname()[1], 443: https.getsockname()[1]}

  def render_http_config(self, **kwargs):
    config = super().render_http_config(**kwargs)
    return re.sub(
      r'(?m)^(\s*listen\s+)(80|443)(?=[\s;])',
      lambda match: f'{match[1]}127.0.0.1:{self.ports[int(match[2])]}',
      config,
    )


@unittest.skipUnless(os.getenv('NGINX_INTEGRATION_BIN'), 'Real nginx integration runs in Linux CI')
class RealNginxTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.directory, self.settings = temporary_settings()
    self.root = Path(self.directory.name)
    self.binary = os.environ['NGINX_INTEGRATION_BIN']
    self.config = self.root / 'nginx.conf'
    module = Path('/usr/lib/nginx/modules/ngx_stream_module.so')
    prefix = f'load_module {module};\n' if module.exists() else ''
    for key in ('NGINX_SITES_ENABLED', 'NGINX_STREAMS_ENABLED'):
      Path(getattr(self.settings, key)).mkdir()
    self.config.write_text(prefix + f'''
pid {self.root}/nginx.pid;
error_log {self.root}/error.log;
events {{ worker_connections 16; }}
http {{
    access_log off;
    include {self.settings.NGINX_SITES_ENABLED}/*.conf;
}}
stream {{
    include {self.settings.NGINX_STREAMS_ENABLED}/*.conf;
}}
''')
    self.settings.NGINX_TEST_COMMAND = [self.binary, '-p', str(self.root) + '/', '-c', str(self.config), '-t']
    self.settings.NGINX_RELOAD_COMMAND = ''
    self.engine, self.factory = create_database(self.settings.DB_URL)
    async with self.engine.begin() as connection:
      await connection.run_sync(Base.metadata.create_all)
    self.session = self.factory()
    self.service = DomainService(settings=self.settings, nginx=UnprivilegedNginx())

  async def asyncTearDown(self):
    await self.session.close()
    await self.engine.dispose()
    self.directory.cleanup()

  async def test_http_disable_excludes_available_and_streams_validate(self):
    row = await self.service.create_domain(self.session, dict(name='example.com', upstream_host='127.0.0.1', upstream_port=3000))
    await self.service.apply_domain(self.session, row['id'])
    await self.service.disable_domain(self.session, row['id'])
    result = subprocess.run([self.binary, '-p', str(self.root) + '/', '-c', str(self.config), '-T'], capture_output=True, text=True)
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertNotIn('server_name example.com', result.stdout)
    for name, protocol, kind in (('tcp-proxy', 'tcp', socket.SOCK_STREAM), ('udp-proxy', 'udp', socket.SOCK_DGRAM)):
      with socket.socket(type=kind) as listener:
        listener.bind(('0.0.0.0', 0))
        port = listener.getsockname()[1]
      row = await self.service.create_domain(self.session, dict(
        type='port_proxy', name=name, stream_protocol=protocol, listen_port=port,
        upstream_host='127.0.0.1', upstream_port=port))
      await self.service.apply_domain(self.session, row['id'])

  async def test_tls_and_acme_template_validate(self):
    openssl = shutil.which('openssl')
    if not openssl:
      self.skipTest('openssl unavailable')
    certificate = Path(self.settings.LETSENCRYPT_DIR) / 'live/example.com'
    certificate.mkdir(parents=True)
    result = subprocess.run([
      openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
      '-subj', '/CN=example.com', '-keyout', str(certificate / 'privkey.pem'),
      '-out', str(certificate / 'fullchain.pem')], capture_output=True, text=True)
    self.assertEqual(result.returncode, 0, result.stderr)
    from backend.models import Domain, DomainCertificate
    from backend.models.certificates import CertificateStatus
    from backend.functions.normalization import normalize_payload
    payload = dict(name='example.com', upstream_host='127.0.0.1', upstream_port=3000)
    row = await self.service.create_domain(self.session, payload)
    domain = await Domain.first(self.session, id=row['id'])
    domain.certificates.append(DomainCertificate(
      cert_name='example.com', server_names=['example.com'], status=CertificateStatus.ACTIVE))
    domain.deployments.append(self.service.build_deployment(normalize_payload(payload), ssl_enabled=True))
    await self.session.commit()
    await self.service.apply_domain(self.session, row['id'])
    expires = await self.service.nginx.certificate_expiry(str(certificate / 'fullchain.pem'), self.settings)
    self.assertIsNotNone(expires)
