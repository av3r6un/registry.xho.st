from pathlib import Path
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def create_database(db_url):
  url = make_url(db_url)
  if url.drivername.startswith('sqlite') and url.database not in (None, '', ':memory:'):
    Path(url.database).parent.mkdir(parents=True, exist_ok=True)
  engine = create_async_engine(db_url, echo=False)
  if url.drivername.startswith('sqlite'):
    @event.listens_for(engine.sync_engine, 'connect')
    def configure_sqlite(connection, _):
      cursor = connection.cursor()
      cursor.execute('PRAGMA foreign_keys=ON')
      cursor.execute('PRAGMA busy_timeout=5000')
      cursor.close()
  return engine, async_sessionmaker(engine, expire_on_commit=False)
