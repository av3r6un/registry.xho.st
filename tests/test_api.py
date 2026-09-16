import os
import unittest
from unittest.mock import patch
from jwt import InvalidTokenError, ExpiredSignatureError, InvalidIssuerError, MissingRequiredClaimError, PyJWKClientError, PyJWKClientConnectionError
from aiohttp.test_utils import TestClient, TestServer
from backend import create_app
from backend.models.base import Base
from backend.services.domain_service import DomainService
from backend.utils.engine import create_database
from tests.support import temporary_settings, FakeNginx


class ApiTests(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self):
    def verify(token, settings):
      if token == 'legacy-access':
        raise MissingRequiredClaimError('token_use')
      if token == 'wrong-issuer':
        raise InvalidIssuerError('Invalid issuer')
      if token == 'refresh-access':
        raise InvalidTokenError('Access token required')
      if token == 'unknown-key':
        raise PyJWKClientError()
      if token == 'jwks-unavailable':
        raise PyJWKClientConnectionError()
      if token == 'expired':
        raise ExpiredSignatureError()
      if token != 'test-access':
        raise InvalidTokenError()
      return {'sub': 'ABC123', 'token_use': 'access'}
    self.verifier = patch('backend.utils.middlewares.verify_access_token', side_effect=verify)
    self.verifier.start()
    self.addCleanup(self.verifier.stop)
    self.directory, settings = temporary_settings()
    self.engine, self.factory = create_database(settings.DB_URL)
    async with self.engine.begin() as connection:
      await connection.run_sync(Base.metadata.create_all)
    app = create_app(settings, session_factory=self.factory,
             domain_service=DomainService(settings=settings, nginx=FakeNginx()))
    self.client = TestClient(TestServer(app))
    await self.client.start_server()
    self.client.session.headers['Authorization'] = 'Bearer test-access'

  async def asyncTearDown(self):
    await self.client.close()
    await self.engine.dispose()
    self.directory.cleanup()

  async def test_api_requires_credentials_and_health_is_public(self):
    response = await self.client.get('/api/domains', headers={'Authorization': ''})
    self.assertEqual(response.status, 401)
    self.assertEqual((await self.client.get('/health', headers={'Authorization': ''})).status, 200)

  async def test_invalid_and_expired_tokens_are_unauthorized(self):
    for token in ('invalid', 'expired', 'unknown-key', ''):
      response = await self.client.get('/api/domains', headers={'Authorization': f'Bearer {token}'})
      self.assertEqual(response.status, 401)

  async def test_jwks_outage_is_service_unavailable(self):
    response = await self.client.get('/api/domains', headers={'Authorization': 'Bearer jwks-unavailable'})
    self.assertEqual(response.status, 503)

  async def test_invalid_token_logs_reason_without_exposing_token(self):
    for token, detail in (
        ('legacy-access', 'MissingRequiredClaimError (missing claim: token_use)'),
        ('wrong-issuer', 'InvalidIssuerError'),
        ('refresh-access', 'InvalidTokenError (access token required)'),
        ('unknown-key', 'PyJWKClientError'),
    ):
      with self.subTest(token=token), self.assertLogs('backend.utils.middlewares', level='WARNING') as logs:
        response = await self.client.get('/api/domains', headers={'Authorization': f'Bearer {token}'})
      self.assertEqual(response.status, 401)
      self.assertEqual((await response.json())['message'], 'Invalid token')
      self.assertIn(detail, logs.output[0])
      self.assertNotIn(token, logs.output[0])

  async def test_authenticated_user_is_created_once_by_uid(self):
    from backend.models import User
    for _ in range(2):
      self.assertEqual((await self.client.get('/api/domains')).status, 200)
    async with self.factory() as session:
      users = await User.all(session)
      self.assertEqual([user.uid for user in users], ['ABC123'])

  async def test_authenticated_create_apply_disable_delete(self):
    data = dict(name='example.com', upstream_host='127.0.0.1', upstream_port=3000)
    response = await self.client.post('/api/domains', json=data)
    self.assertEqual(response.status, 201)
    domain_id = (await response.json())['body']['id']
    for action in ('apply', 'disable'):
      response = await self.client.post(f'/api/domains/{domain_id}/{action}')
      self.assertEqual(response.status, 200)
    self.assertEqual((await self.client.delete(f'/api/domains/{domain_id}')).status, 200)

  async def test_no_frontend_even_when_static_dir_is_set(self):
    with patch.dict(os.environ, {'STATIC_DIR': self.directory.name}):
      for url in ('/', '/domains/new', '/assets/app.js', '/api/missing'):
        response = await self.client.get(url)
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())['status'], 'error')

  async def test_validation_not_found_and_conflict(self):
    for value in ([], 'not-an-object', None):
      response = await self.client.post('/api/domains', json=value)
      self.assertIn(response.status, (400, 415))
    response = await self.client.get('/api/domains/999')
    self.assertEqual(response.status, 404)
    response = await self.client.get('/api/domains/nope')
    self.assertEqual(response.status, 400)
    data = dict(name='example.com', upstream_host='127.0.0.1', upstream_port=3000)
    self.assertEqual((await self.client.post('/api/domains', json=data)).status, 201)
    self.assertEqual((await self.client.post('/api/domains', json=data)).status, 409)

  async def test_origin_does_not_block_api_requests(self):
    headers = {'Origin': 'https://panel.example.com'}
    self.assertEqual((await self.client.get('/api/domains', headers=headers)).status, 200)
    data = dict(name='example.com', upstream_host='127.0.0.1', upstream_port=3000)
    response = await self.client.post('/api/domains', json=data, headers=headers)
    self.assertEqual(response.status, 201)
    domain_id = (await response.json())['body']['id']
    response = await self.client.put(f'/api/domains/{domain_id}', json={'upstream_port': 4000}, headers=headers)
    self.assertEqual(response.status, 200)
    response = await self.client.post(f'/api/domains/{domain_id}/apply', headers=headers)
    self.assertEqual(response.status, 200)
    self.assertEqual((await self.client.delete(f'/api/domains/{domain_id}', headers=headers)).status, 200)
