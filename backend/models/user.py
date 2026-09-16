from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String
from .base import Base


class User(Base):
  __tablename__ = 'users'
  
  uid: Mapped[str] = mapped_column(String(6), primary_key=True)
  
  def __init__(self, uid, **kwargs) -> None:
    self.uid = uid
    
  @property
  def json(self):
    return dict(uid=self.uid)
  
  def __str__(self) -> str:
    return f'<User {self.uid}>'