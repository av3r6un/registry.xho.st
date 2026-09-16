from backend.app_keys import SETTINGS, DOMAIN_SERVICE, SESSION_FACTORY
from aiohttp import web
from sqlalchemy import text

main = web.RouteTableDef()


@main.get('/health')
async def check_health(req):
  try:
    async with req.app[SESSION_FACTORY]() as session:
      await session.execute(text('SELECT 1 FROM domains LIMIT 1'))
    return web.json_response({'status': 'success', 'message': 'Healthy'})
  except Exception:
    return web.json_response({'status': 'error', 'message': 'database_unavailable'}, status=503)
