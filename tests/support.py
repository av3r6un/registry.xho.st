from backend.utils.time import utcnow
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from backend.config import Settings
from backend.services.nginx import Nginx


def temporary_settings():
  directory = tempfile.TemporaryDirectory()
  root = Path(directory.name)
  settings = Settings(
    IMPORT_NGINX_CONFIGS=False,
    DB_URL='sqlite+aiosqlite:///:memory:',
    NGINX_SITES_AVAILABLE=str(root / 'sites-available'),
    NGINX_SITES_ENABLED=str(root / 'sites-enabled'),
    NGINX_STREAMS_AVAILABLE=str(root / 'streams-available'),
    NGINX_STREAMS_ENABLED=str(root / 'streams-enabled'),
    NGINX_BACKUP_DIR=str(root / 'backups'),
    CERTBOT_WEBROOT=str(root / 'webroot'),
    LETSENCRYPT_DIR=str(root / 'certificates'),
    CERTBOT_WORK_DIR=str(root / 'certbot-work'),
    CERTBOT_LOGS_DIR=str(root / 'certbot-logs'),
  )
  return directory, settings


class FakeNginx(Nginx):
  def __init__(self):
    super().__init__()
    self.test_results = []
    self.reload_results = []
    self.certificate_success = True
    self.challenge_text = None
    self.commands = []

  def result(self, success):
    return dict(success=success, stdout='', stderr='' if success else 'simulated_failure', returncode=0 if success else 1)

  async def test(self, settings):
    self.commands.append('test')
    return self.result(self.test_results.pop(0) if self.test_results else True)

  async def reload(self, settings):
    self.commands.append('reload')
    return self.result(self.reload_results.pop(0) if self.reload_results else True)

  async def issue_certificate(self, name, names, settings):
    self.challenge_text = next(Path(settings.NGINX_SITES_ENABLED).glob('*.conf')).read_text()
    return self.result(self.certificate_success)

  async def certificate_expiry(self, fullchain, settings):
    return utcnow() + timedelta(days=90)
