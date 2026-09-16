import asyncio
import logging

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, web
from yarl import URL

from backend.app_keys import AUTH_CLIENT, SETTINGS

logger = logging.getLogger(__name__)
auth = web.RouteTableDef()
AUTH_PATHS = {'/api/auth': '/', '/api/auth/': '/', '/api/auth/refresh': '/refresh'}


def is_auth_proxy_request(req):
  return req.method == 'POST' and req.path in AUTH_PATHS


async def auth_client_ctx(app):
  target = URL(app[SETTINGS].AUTH_PROXY_TARGET or f'https://{app[SETTINGS].AUTH_SERVER}')
  if (target.scheme not in ('http', 'https') or not target.host or target.user is not None
      or target.path not in ('', '/') or target.query_string or target.fragment):
    raise ValueError('invalid_auth_proxy_target')
  async with ClientSession(timeout=ClientTimeout(total=20), cookie_jar=DummyCookieJar()) as client:
    app[AUTH_CLIENT] = client
    yield


@auth.post('/api/auth')
@auth.post('/api/auth/')
@auth.post('/api/auth/refresh')
async def proxy_auth(req):
  settings = req.app[SETTINGS]
  target = URL(settings.AUTH_PROXY_TARGET or f'https://{settings.AUTH_SERVER}').with_path(AUTH_PATHS[req.path])
  # No browser cookies or Authorization headers are shared with the auth service.
  headers = {'Content-Type': req.headers.get('Content-Type', 'application/json')}
  if req.headers.get('User-Agent'):
    headers['User-Agent'] = req.headers['User-Agent']
  body = await req.read()
  try:
    async with req.app[AUTH_CLIENT].post(target, data=body, headers=headers, allow_redirects=False) as response:
      if 300 <= response.status < 400:
        raise web.HTTPBadGateway(reason='Unexpected authentication service redirect')
      return web.Response(
        status=response.status,
        body=await response.read(),
        headers={'Content-Type': response.headers.get('Content-Type', 'application/json'),
                 'Cache-Control': 'no-store'},
      )
  except (ClientError, asyncio.TimeoutError) as error:
    logger.warning('Authentication proxy unavailable: %s', type(error).__name__)
    raise web.HTTPServiceUnavailable(reason='Authentication service unavailable')
