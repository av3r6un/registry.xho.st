from .domains import domains
from .main import main
from .auth import auth

rts = (
  *domains,
  *main,
  *auth,
)
