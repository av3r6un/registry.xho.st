import asyncio
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.config import Settings
from backend.models import Domain, DomainCertificate, DomainDeployment, DomainRoute, DomainServerName
from backend.models.base import Base

config = context.config
if config.config_file_name is not None:
  fileConfig(config.config_file_name)

target_metadata = Base.metadata
db_url = Settings().DB_URL
url = make_url(db_url)
if url.drivername.startswith('sqlite') and url.database not in (None, '', ':memory:'):
  Path(url.database).parent.mkdir(parents=True, exist_ok=True)
config.set_main_option('sqlalchemy.url', db_url.replace('%', '%%'))


def do_run_migrations(connection):
  context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
  with context.begin_transaction():
    context.run_migrations()


def run_migrations_offline():
  context.configure(
    url=config.get_main_option('sqlalchemy.url'), target_metadata=target_metadata,
    literal_binds=True, compare_type=True, dialect_opts={'paramstyle': 'named'})
  with context.begin_transaction():
    context.run_migrations()


async def run_async_migrations():
  engine = async_engine_from_config(
    config.get_section(config.config_ini_section), prefix='sqlalchemy.', poolclass=pool.NullPool)
  try:
    async with engine.connect() as connection:
      await connection.run_sync(do_run_migrations)
  finally:
    await engine.dispose()


if context.is_offline_mode():
  run_migrations_offline()
else:
  asyncio.run(run_async_migrations())
