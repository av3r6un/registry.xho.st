from backend.app_keys import SETTINGS, DOMAIN_SERVICE, SESSION_FACTORY
from aiohttp import web

domains = web.RouteTableDef()


def domain_id(req):
  try:
    value = int(req.match_info['id'])
  except ValueError:
    raise ValueError('invalid_domain_id') from None
  if value < 1:
    raise ValueError('invalid_domain_id')
  return value


async def payload(req):
  if req.content_type != 'application/json':
    raise web.HTTPUnsupportedMediaType(reason='application_json_required')
  try:
    data = await req.json()
  except (ValueError, UnicodeError):
    raise ValueError('invalid_json') from None
  if not isinstance(data, dict):
    raise ValueError('payload_must_be_object')
  return data


def success(body, status=200):
  return web.json_response({'status': 'success', 'body': body}, status=status)


@domains.get('/api/domains')
async def index(req):
  return success(await req.app[DOMAIN_SERVICE].all(req['session']))


@domains.get('/api/domains/{id}')
async def get_domain(req):
  return success((await req.app[DOMAIN_SERVICE].find(req['session'], id=domain_id(req))).json)


@domains.post('/api/domains')
async def create_domain(req):
  return success(await req.app[DOMAIN_SERVICE].create_domain(req['session'], await payload(req)), 201)


@domains.put('/api/domains/{id}')
async def update_domain(req):
  data = await payload(req)
  data['id'] = domain_id(req)
  return success(await req.app[DOMAIN_SERVICE].update_domain(req['session'], data))


@domains.post('/api/domains/{id}/apply')
async def apply_domain(req):
  return success(await req.app[DOMAIN_SERVICE].apply_domain(req['session'], domain_id(req)))


@domains.post('/api/domains/{id}/certificate')
async def issue_certificate(req):
  return success(await req.app[DOMAIN_SERVICE].issue_certificate(req['session'], domain_id(req)))


@domains.post('/api/domains/{id}/disable')
async def disable_domain(req):
  return success(await req.app[DOMAIN_SERVICE].disable_domain(req['session'], domain_id(req)))


@domains.delete('/api/domains/{id}')
async def delete_domain(req):
  return success(await req.app[DOMAIN_SERVICE].delete(req['session'], domain_id(req)))
