from aiohttp.web import AppKey
from backend.config import Settings
from sqlalchemy.ext.asyncio import async_sessionmaker
from aiohttp import ClientSession

SETTINGS = AppKey('settings', Settings)
DOMAIN_SERVICE = AppKey('domain_service', object)
SESSION_FACTORY = AppKey('db_sessionmaker', async_sessionmaker)
AUTH_CLIENT = AppKey('auth_client', ClientSession)
