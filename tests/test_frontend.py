import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from backend import create_app
from backend.models.base import Base
from backend.services.domain_service import DomainService
from backend.utils.engine import create_database
from tests.support import FakeNginx, temporary_settings


class FrontendTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    self.directory, self.settings = temporary_settings()
    root = Path(self.directory.name)
    self.frontend = root / 'frontend'
    (self.frontend / 'static').mkdir(parents=True)
    (self.frontend / 'img').mkdir()
    (self.frontend / 'index.html').write_text('<html>Vue application</html>', encoding='utf-8')
    (self.frontend / 'static/app.js').write_text('window.app = true;', encoding='utf-8')
    (self.frontend / 'img/logo.png').write_bytes(b'PNG')
    (self.frontend / 'favicon.ico').write_bytes(b'ICO')
    (root / 'secret.txt').write_text('PRIVATE', encoding='utf-8')
    self.upstream_requests = []
    self.upstream_status = 200

    async def upstream(req):
      self.upstream_requests.append((req.path, await req.json(), dict(req.headers)))
      response = web.json_response({'status': 'ok', 'body': {'access_token': 'access'}},
                                   status=self.upstream_status)
      response.set_cookie('auth_session', 'must-not-be-shared')
      if self.upstream_status == 302:
        response.headers['Location'] = '/refresh'
      return response

    auth_app = web.Application()
    auth_app.router.add_post('/', upstream)
    auth_app.router.add_post('/refresh', upstream)
    self.auth_server = TestServer(auth_app)
    await self.auth_server.start_server()
    self.settings.AUTH_PROXY_TARGET = str(self.auth_server.make_url('/'))
    self.engine, self.factory = create_database(self.settings.DB_URL)
    async with self.engine.begin() as connection:
      await connection.run_sync(Base.metadata.create_all)
    verifier = patch('backend.utils.middlewares.verify_access_token', return_value={'sub': 'ABC123'})
    self.verifier = verifier.start()
    self.addCleanup(verifier.stop)
    app = create_app(self.settings, session_factory=self.factory, frontend_dir=self.frontend,
                     domain_service=DomainService(settings=self.settings, nginx=FakeNginx()))
    self.client = TestClient(TestServer(app))
    await self.client.start_server()

  async def asyncTearDown(self):
    await self.client.close()
    await self.auth_server.close()
    await self.engine.dispose()
    self.directory.cleanup()

  async def test_pages_and_assets_are_public_and_support_head(self):
    for path in ('/', '/auth', '/domains/new', '/domains/edit/42', '/index.html'):
      response = await self.client.get(path)
      self.assertEqual(response.status, 200)
      self.assertEqual(response.content_type, 'text/html')
      self.assertEqual(await response.text(), '<html>Vue application</html>')
      self.assertEqual(response.headers['Cache-Control'], 'no-cache')
    for path, content in (('/static/app.js', b'window.app = true;'),
                          ('/img/logo.png', b'PNG'), ('/favicon.ico', b'ICO')):
      response = await self.client.get(path)
      self.assertEqual(response.status, 200)
      self.assertEqual(await response.read(), content)
    response = await self.client.head('/auth')
    self.assertEqual(response.status, 200)
    self.assertEqual(await response.read(), b'')
    self.verifier.assert_not_called()

  async def test_missing_assets_and_traversal_stay_not_found(self):
    for path in ('static/missing.js', 'img/missing.png', '.env',
                 'static/%2e%2e/%2e%2e/secret.txt', '%2e%2e/secret.txt'):
      url = URL(str(self.client.server.make_url('/')) + path, encoded=True)
      response = await self.client.session.get(url)
      self.assertEqual(response.status, 404)
      self.assertNotIn('PRIVATE', await response.text())

  async def test_api_keeps_authentication_and_never_returns_spa(self):
    self.assertEqual((await self.client.get('/api/domains')).status, 401)
    for path in ('/api', '/api/missing', '/api/auth/missing'):
      response = await self.client.get(path, headers={'Authorization': 'Bearer access'})
      self.assertEqual(response.status, 404)
      self.assertEqual(response.content_type, 'application/json')
    self.assertEqual((await self.client.post('/domains/new')).status, 405)

  async def test_login_and_refresh_forward_contract_without_shared_credentials(self):
    for path, target, data in (
        ('/api/auth/', '/', {'email': 'user@example.com', 'password': 'password'}),
        ('/api/auth/refresh', '/refresh', {'refresh_token': 'refresh'}),
    ):
      response = await self.client.post(path, json={'data': data},
                                       headers={'Authorization': 'Bearer unrelated', 'Cookie': 'private=value'})
      self.assertEqual(response.status, 200)
      self.assertEqual((await response.json())['body']['access_token'], 'access')
      self.assertEqual(response.headers['Cache-Control'], 'no-store')
      self.assertNotIn('Set-Cookie', response.headers)
      actual_path, actual_body, actual_headers = self.upstream_requests[-1]
      self.assertEqual(actual_path, target)
      self.assertEqual(actual_body, {'data': data})
      self.assertNotIn('Cookie', actual_headers)
      self.assertNotIn('Authorization', actual_headers)
    self.verifier.assert_not_called()

  async def test_auth_errors_are_preserved_and_redirects_are_rejected(self):
    self.upstream_status = 401
    response = await self.client.post('/api/auth', json={'data': {}})
    self.assertEqual(response.status, 401)
    self.assertEqual((await response.json())['status'], 'ok')
    self.upstream_status = 302
    response = await self.client.post('/api/auth', json={'data': {}})
    self.assertEqual(response.status, 502)
    self.assertEqual(len(self.upstream_requests), 2)

  async def test_auth_outage_returns_service_unavailable(self):
    await self.auth_server.close()
    response = await self.client.post('/api/auth/', json={'data': {}})
    self.assertEqual(response.status, 503)
    self.assertEqual((await response.json())['message'], 'Authentication service unavailable')

  async def test_auth_target_is_fixed_and_unlisted_endpoints_are_not_proxied(self):
    response = await self.client.post('/api/auth/register', json={'data': {}})
    self.assertEqual(response.status, 405)
    self.assertEqual(self.upstream_requests, [])
    self.settings.AUTH_PROXY_TARGET = 'https://auth.example.com?target=elsewhere'
    app = create_app(self.settings, session_factory=self.factory)
    client = TestClient(TestServer(app))
    try:
      with self.assertRaisesRegex(ValueError, 'invalid_auth_proxy_target'):
        await client.start_server()
    finally:
      await client.close()
