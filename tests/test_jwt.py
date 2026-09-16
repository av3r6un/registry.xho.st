import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.utils.jwt import verify_access_token


class JwtTests(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cls.settings = SimpleNamespace(AUTH_SERVER='auth.example.com')

  def token(self, **overrides):
    now = datetime.now(timezone.utc)
    payload = dict(sub='ABC123', iss=self.settings.AUTH_SERVER, iat=now, token_use='access',
                   exp=now + timedelta(minutes=5))
    payload.update(overrides)
    return jwt.encode(payload, self.key, algorithm='RS256')

  def verify(self, token):
    client = SimpleNamespace(get_signing_key_from_jwt=lambda token:
                             SimpleNamespace(key=self.key.public_key()))
    with patch('backend.utils.jwt.jwk_client_for', return_value=client):
      return verify_access_token(token, self.settings)

  def test_valid_rsa_token(self):
    self.assertEqual(self.verify(self.token())['sub'], 'ABC123')

  def test_refresh_and_invalid_token_use_are_rejected(self):
    for use in ('refresh', 'unknown', '', None):
      with self.subTest(token_use=use), self.assertRaises(jwt.InvalidTokenError):
        self.verify(self.token(token_use=use))

  def test_legacy_token_without_token_use_is_rejected(self):
    now = datetime.now(timezone.utc)
    legacy = jwt.encode(dict(sub='ABC123', iss=self.settings.AUTH_SERVER, iat=now,
                             exp=now + timedelta(minutes=5)), self.key, algorithm='RS256')
    with self.assertRaises(jwt.MissingRequiredClaimError):
      self.verify(legacy)

  def test_wrong_issuer_expired_and_invalid_signature(self):
    with self.assertRaises(jwt.InvalidIssuerError):
      self.verify(self.token(iss='another.example.com'))
    with self.assertRaises(jwt.ExpiredSignatureError):
      self.verify(self.token(exp=datetime.now(timezone.utc) - timedelta(seconds=1)))
    token = self.token()
    signature = token.rsplit('.', 1)[1]
    signature = ('A' if signature[0] != 'A' else 'B') + signature[1:]
    with self.assertRaises(jwt.InvalidSignatureError):
      self.verify(token.rsplit('.', 1)[0] + '.' + signature)
