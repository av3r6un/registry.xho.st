from __future__ import annotations
import asyncio
import logging

from jwt import ExpiredSignatureError, InvalidTokenError, MissingRequiredClaimError, PyJWKClientError, PyJWKClientConnectionError

from aiohttp import web
from sqlalchemy.exc import IntegrityError

from backend.app_keys import SESSION_FACTORY, SETTINGS

from .jwt import verify_access_token

logger = logging.getLogger(__name__)


@web.middleware
async def errors_middleware(req, handler):
  try:
    return await handler(req)
  except web.HTTPException as error:
    return web.json_response({'status': 'error', 'message': error.reason}, status=error.status)
  except ValueError as error:
    from backend.services.domain_service import ConflictError
    return web.json_response({'status': 'error', 'message': str(error)}, status=409 if isinstance(error, ConflictError) else 400)
  except IntegrityError:
    return web.json_response({'status': 'error', 'message': 'domain_or_route_conflict'}, status=409)
  except Exception as error:
    from backend.services.domain_service import DomainNotFound
    from backend.services.nginx import NginxOperationError
    if isinstance(error, DomainNotFound):
      return web.json_response({'status': 'error', 'message': 'domain_not_found'}, status=404)
    if isinstance(error, NginxOperationError):
      logger.warning('Nginx operation failed: %s', error)
      return web.json_response({'status': 'error', 'message': str(error)}, status=502)
    logger.exception('Unhandled API error')
    return web.json_response({'status': 'error', 'message': 'internal_server_error'}, status=500)


@web.middleware
async def db_middleware(req, handler):
  if req.path != '/api' and not req.path.startswith('/api/'):
    return await handler(req)
  async with req.app[SESSION_FACTORY]() as session:
    req['session'] = session
    try:
      return await handler(req)
    finally:
      # Services own their transactions; pending work is never implicitly committed.
      await session.rollback()


@web.middleware
async def jwt_middleware(req: web.Request, handler):
  from backend.services import AuthService
  from backend.models import User
  
  route_error = getattr(req.match_info, 'http_exception', None)
  if route_error is not None:
    raise route_error
  
  settings = req.app[SETTINGS]
  if any(req.path == path or req.path.startswith(path.rstrip('/') + '/')
         for path in settings.NOT_SECURED_PATHS):
    return await handler(req)
  
  auth_header = req.headers.get('Authorization')
  scheme, _, token = (auth_header or '').partition(' ')
  if scheme.lower() != 'bearer' or not token.strip():
    raise web.HTTPUnauthorized(reason='Bearer token required')

  try:
    payload = await asyncio.to_thread(verify_access_token, token.strip(), settings)
    print(payload)
    session = req['session']
    user = await AuthService.get_user(session, payload['sub'])
    if not user:
      user = User(payload['sub'])
      await user.save(session)

    req['current_user'] = user
  
  except ExpiredSignatureError:
    raise web.HTTPUnauthorized(reason='Token expired')
  except PyJWKClientConnectionError:
    raise web.HTTPServiceUnavailable(reason='Authentication service unavailable')
  except (InvalidTokenError, PyJWKClientError) as error:
    detail = type(error).__name__
    if isinstance(error, MissingRequiredClaimError) and error.claim in ('exp', 'iat', 'iss', 'sub', 'token_use'):
      detail += f' (missing claim: {error.claim})'
    elif type(error) is InvalidTokenError and str(error) == 'Access token required':
      detail += ' (access token required)'
    logger.warning('JWT rejected for %s: %s; expected issuer: %s', req.path, detail, settings.AUTH_SERVER)
    raise web.HTTPUnauthorized(reason='Invalid token')
  
  return await handler(req)

middlewares = [errors_middleware, db_middleware, jwt_middleware]
