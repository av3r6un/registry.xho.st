from backend.utils.time import utcnow
from datetime import datetime, date
from sqlalchemy import inspect, select, func, DateTime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, selectinload


class Base(DeclarativeBase):
  created: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
  updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

  @classmethod
  async def get(cls, session: AsyncSession, **filters):
    query = select(cls)
    for relation in inspect(cls).relationships:
      query = query.options(selectinload(getattr(cls, relation.key)))
    for key, value in filters.items():
      field, _, operation = key.partition('__')
      column = getattr(cls, field)
      if not operation:
        expression = column == value
      elif operation == 'date':
        expression = func.date(column) == (value if isinstance(value, date) else date.fromisoformat(value))
      elif operation in ('gte', 'lte', 'gt', 'lt', 'like', 'ilike'):
        methods = {'gte': '__ge__', 'lte': '__le__', 'gt': '__gt__', 'lt': '__lt__', 'like': 'like', 'ilike': 'ilike'}
        expression = getattr(column, methods[operation])(f'%{value}%' if operation == 'ilike' else value)
      elif operation in ('isnull', 'notnull'):
        expression = column.is_(None) if operation == 'isnull' else column.is_not(None)
      else:
        raise ValueError('unsupported_filter_operation')
      query = query.where(expression)
    result = await session.execute(query.order_by(*inspect(cls).primary_key))
    return result.scalars()

  @classmethod
  async def get_json(cls, session, **filters):
    return [row.json for row in await cls.get(session, **filters)]

  @classmethod
  async def first(cls, session, **filters):
    return (await cls.get(session, **filters)).first()

  @classmethod
  async def all(cls, session, **filters):
    return (await cls.get(session, **filters)).all()

  async def save(self, session: AsyncSession):
    session.add(self)
    await session.commit()
