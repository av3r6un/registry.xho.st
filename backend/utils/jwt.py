import jwt
from functools import lru_cache


@lru_cache(maxsize=8)
def jwk_client_for(auth_server: str):
  return jwt.PyJWKClient(f'https://{auth_server}/.well-known/jwks.json')


def verify_access_token(token: str, settings) -> dict:
  jwk_client = jwk_client_for(settings.AUTH_SERVER)
  signing_key = jwk_client.get_signing_key_from_jwt(token).key
  payload = jwt.decode(
    token,
    signing_key,
    algorithms=['RS256'],
    issuer=settings.AUTH_SERVER,
    options={'require': ['exp', 'iat', 'iss', 'sub', 'token_use']}
  )
  print(signing_key)
  if payload['token_use'] != 'access':
    raise jwt.InvalidTokenError('Access token required')
  return payload
