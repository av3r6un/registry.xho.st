import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from yaml import safe_load

ROOT = Path(__file__).resolve().parents[2]


def parse_bool(value):
  if isinstance(value, bool):
    return value
  text = str(value).strip().lower()
  if text in ('1', 'true', 'yes', 'on'):
    return True
  if text in ('0', 'false', 'no', 'off', ''):
    return False
  raise ValueError(f'invalid_boolean: {value!r}')


class Settings:
  SETTING_FP = Path(__file__).with_name('settings.yaml')
  DEFAULTS = {
    'DB_URL': f'sqlite+aiosqlite:///{(ROOT / "backend/data/app.db").as_posix()}',
    'APP_HOST': '127.0.0.1', 'APP_PORT': 8090, 'DEBUG': False,
    'NGINX_TEST_COMMAND': '/usr/sbin/nginx -t',
    'NGINX_RELOAD_COMMAND': '/usr/sbin/nginx -s reload',
    'NGINX_SITES_AVAILABLE': '/etc/nginx/sites-available',
    'NGINX_SITES_ENABLED': '/etc/nginx/sites-enabled',
    'NGINX_STREAMS_AVAILABLE': '/etc/nginx/streams-available',
    'NGINX_STREAMS_ENABLED': '/etc/nginx/streams-enabled',
    'NGINX_BACKUP_DIR': str(ROOT / 'backend/runtime/backups'),
    'NGINX_COMMAND_TIMEOUT': 30, 'CERTBOT_COMMAND_TIMEOUT': 300,
    'CERTBOT_BIN': '/usr/bin/certbot', 'CERTBOT_EMAIL': '',
    'CERTBOT_WEBROOT': '/var/www/certbot', 'CERTBOT_STAGING': False,
    'LETSENCRYPT_DIR': '/etc/letsencrypt',
    'CERTBOT_WORK_DIR': '/var/lib/letsencrypt', 'CERTBOT_LOGS_DIR': '/var/log/letsencrypt',
    'OPENSSL_BIN': '/usr/bin/openssl', 'IMPORT_NGINX_CONFIGS': True,
    'AUTH_SERVER': 'id.xho.st',
    'AUTH_PROXY_TARGET': '',
    'NOT_SECURED_PATHS': ['/health'],
  }

  def __init__(self, **overrides):
    load_dotenv(ROOT / '.env', override=False)
    with open(self.SETTING_FP, encoding='utf-8') as source:
      yaml_settings = safe_load(source) or {}
    unknown = (yaml_settings.keys() | overrides.keys()) - self.DEFAULTS.keys()
    if unknown:
      raise ValueError(f'unknown_settings: {", ".join(sorted(unknown))}')
    for key, default in self.DEFAULTS.items():
      value = overrides.get(key, os.environ.get(key, yaml_settings.get(key, default)))
      if isinstance(default, bool):
        value = parse_bool(value)
      elif isinstance(default, int):
        value = int(value)
      setattr(self, key, value)
    if not 1 <= self.APP_PORT <= 65535:
      raise ValueError('invalid_app_port')
    if self.NGINX_COMMAND_TIMEOUT <= 0 or self.CERTBOT_COMMAND_TIMEOUT <= 0:
      raise ValueError('invalid_command_timeout')
    url = make_url(self.DB_URL)
    if url.drivername.startswith('sqlite') and url.database not in (None, '', ':memory:'):
      path = Path(url.database)
      if not path.is_absolute():
        path = ROOT / path
      self.DB_URL = url.set(database=str(path.resolve())).render_as_string(hide_password=False)
