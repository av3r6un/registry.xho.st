from backend.models import User


class AuthService:
  
  @classmethod
  async def get_user(cls, session, uid) -> User | None:
    user = await User.first(session, uid=uid)
    return user
