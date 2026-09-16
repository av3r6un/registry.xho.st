from backend.app_keys import SETTINGS, DOMAIN_SERVICE, SESSION_FACTORY
import logging
from aiohttp import web
from .config import Settings
from .services.domain_service import DomainService
from .utils.engine import create_database
from .utils.middlewares import middlewares
from .config.config import ROOT
from .routes.auth import auth_client_ctx
from .routes.frontend import register_frontend


async def db_ctx(app):
  engine = None
  if SESSION_FACTORY not in app:
    engine, factory = create_database(app[SETTINGS].DB_URL)
    app[SESSION_FACTORY] = factory
  try:
    if app[SETTINGS].IMPORT_NGINX_CONFIGS:
      from .services.config_importer import NginxConfigImporter
      async with app[DOMAIN_SERVICE].lock:
        async with app[SESSION_FACTORY]() as session:
          stats = await NginxConfigImporter(app[SETTINGS]).import_current_configs(session)
          logging.info('Nginx config import completed: %s', stats)
    yield
  finally:
    if engine is not None:
      await engine.dispose()


def create_app(settings=None, *, session_factory=None, domain_service=None, frontend_dir=None):
  from .routes import rts
  settings = settings or Settings()
  app = web.Application(middlewares=middlewares)
  app[SETTINGS] = settings
  app[DOMAIN_SERVICE] = domain_service or DomainService(settings=settings)
  if session_factory is not None:
    app[SESSION_FACTORY] = session_factory
  app.add_routes(rts)
  register_frontend(app, frontend_dir if frontend_dir is not None else ROOT / 'frontend')
  app.cleanup_ctx.append(auth_client_ctx)
  app.cleanup_ctx.append(db_ctx)
  return app


def start():
  settings = Settings()
  logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
  )
  web.run_app(create_app(settings), host=settings.APP_HOST, port=settings.APP_PORT)
