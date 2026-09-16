from backend.utils.time import utcnow
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from backend.functions.normalization import normalize_payload
from backend.models import Domain, DomainCertificate, DomainDeployment, DomainRoute, DomainServerName
from backend.models.certificates import CertificateStatus
from backend.models.deployments import DeploymentStatus
from backend.models.domains import DomainStatus, DomainType

logger = logging.getLogger(__name__)


@dataclass
class ImportedConfig:
  payload: dict
  config_text: str
  filename: str
  available_path: str
  enabled_path: str
  ssl_enabled: bool = False
  cert_name: str | None = None


class NginxConfigImporter:
  """Import new, simple snippets. Existing managed domains and drafts are authoritative."""

  def __init__(self, settings):
    self.settings = settings
    self.parse_stats = dict(skipped=0, errors=0)

  async def import_current_configs(self, session):
    self.parse_stats = dict(skipped=0, errors=0)
    configs = self.parse_current_configs()
    stats = dict(imported=0, updated=0, **self.parse_stats)
    for config in configs:
      try:
        async with session.begin_nested():
          action = await self.upsert_config(session, config)
          await session.flush()
        stats[action] += 1
      except Exception:
        stats['errors'] += 1
        logger.exception('Failed to import nginx config: %s', config.enabled_path)
    await session.commit()
    return stats

  def parse_current_configs(self):
    return [*self.parse_http_configs(), *self.parse_stream_configs()]

  def parse_http_configs(self):
    return self.parse_files(self.config_files(getattr(self.settings, 'NGINX_SITES_ENABLED', '')), self.parse_http_file)

  def parse_stream_configs(self):
    return self.parse_files(self.config_files(getattr(self.settings, 'NGINX_STREAMS_ENABLED', '')), self.parse_stream_file)

  def config_files(self, directory):
    path = Path(directory) if directory else None
    if path is None or not path.is_dir():
      return []
    return sorted(a for a in path.iterdir() if a.is_file() and a.suffix == '.conf')

  def parse_files(self, paths, parser):
    configs = []
    for path in paths:
      try:
        parsed = parser(path)
        configs.extend(parsed)
        if not parsed:
          self.parse_stats['skipped'] += 1
          logger.info('Skipping unsupported nginx snippet: %s', path)
      except Exception:
        self.parse_stats['errors'] += 1
        logger.exception('Failed to parse nginx config: %s', path)
    return configs

  def parse_http_file(self, path):
    text = path.read_text(encoding='utf-8')
    blocks = self.server_blocks(text)
    proxy_blocks = [block for block in blocks if self.directive(block, 'proxy_pass')]
    if len(proxy_blocks) != 1 or len(blocks) > 2:
      return []
    block = proxy_blocks[0]
    values = re.findall(r'\bproxy_pass\s+([^;]+);', block)
    if len(values) != 1:
      return []
    match = re.fullmatch(r'(?P<scheme>https?)://(?P<host>\[[^\]]+\]|[^:/\s;]+):(?P<port>\d+)', values[0].strip())
    names = self.server_names(block)
    if not match or not names:
      return []
    if any(set(self.server_names(a)) != set(names) for a in blocks):
      return []
    ssl_blocks = [a for a in blocks if self.block_has_ssl(a)]
    if ssl_blocks and ssl_blocks != [block]:
      return []
    cert_name = self.cert_name(block) if ssl_blocks else None
    if ssl_blocks and not cert_name:
      return []
    payload = normalize_payload(dict(
      type='hostname', name=names[0], server_names=names, upstream_scheme=match.group('scheme'),
      upstream_host=match.group('host'), upstream_port=int(match.group('port'))))
    return [ImportedConfig(
      payload, text, path.name, self.available_path('http', path.name), str(path),
      ssl_enabled=bool(ssl_blocks), cert_name=cert_name)]

  def parse_stream_file(self, path):
    text = path.read_text(encoding='utf-8')
    blocks = self.server_blocks(text)
    # A file is the unit of apply/disable. Multiple stream servers cannot safely be one row.
    if len(blocks) != 1:
      return []
    listen = self.directive(blocks[0], 'listen')
    upstream = self.directive(blocks[0], 'proxy_pass')
    listen_match = re.fullmatch(r'(?P<port>\d+)(?:\s+(?P<protocol>udp))?', listen or '')
    upstream_match = re.fullmatch(r'(?P<host>\[[^\]]+\]|[^:/\s;]+):(?P<port>\d+)', upstream or '')
    if not listen_match or not upstream_match:
      return []
    payload = normalize_payload(dict(
      type='port_proxy', name=path.stem, listen_port=int(listen_match.group('port')),
      stream_protocol=listen_match.group('protocol') or 'tcp',
      upstream_host=upstream_match.group('host'), upstream_port=int(upstream_match.group('port'))))
    return [ImportedConfig(payload, text, path.name, self.available_path('stream', path.name), str(path))]

  async def upsert_config(self, session, config):
    if await Domain.first(session, name=config.payload['domain']['name']):
      return 'skipped'
    query = select(DomainDeployment.id).where(
      (DomainDeployment.sites_enabled_path == config.enabled_path) |
      (DomainDeployment.sites_available_path == config.available_path))
    if (await session.execute(query)).first():
      return 'skipped'
    from .domain_service import DomainService
    # Test settings may only include paths; conflict checks for streams need APP_PORT.
    if not hasattr(self.settings, 'APP_PORT'):
      self.settings.APP_PORT = 8090
    await DomainService(settings=self.settings).check_conflicts(session, config.payload)
    domain = Domain(**config.payload['domain'])
    domain.enabled = True
    domain.status = DomainStatus.ACTIVE
    domain.route = DomainRoute(**config.payload['route'])
    domain.server_names = [DomainServerName(**a) for a in config.payload['server_names']]
    domain.deployments = [self.build_deployment(config)]
    domain.certificates = []
    if config.ssl_enabled:
      certificate = self.build_certificate(config, domain.name)
      from .nginx import Nginx, NginxOperationError
      try:
        certificate.expires_at = await Nginx().certificate_expiry(certificate.fullchain_path, self.settings)
        if certificate.expires_at <= utcnow():
          certificate.status = CertificateStatus.EXPIRED
      except (NginxOperationError, AttributeError):
        certificate.status = CertificateStatus.ERROR
        certificate.last_error = 'certificate_metadata_unavailable'
      domain.certificates.append(certificate)
    session.add(domain)
    return 'imported'

  def build_deployment(self, config):
    return DomainDeployment(
      status=DeploymentStatus.APPLIED, nginx_filename=config.filename,
      sites_available_path=config.available_path, sites_enabled_path=config.enabled_path,
      config_text=config.config_text, ssl_enabled=config.ssl_enabled, cert_name=config.cert_name,
      applied_at=utcnow())

  def build_certificate(self, config, domain_name):
    cert_name = config.cert_name or domain_name
    return DomainCertificate(
      provider='certbot', cert_name=cert_name,
      server_names=[a['name'] for a in config.payload['server_names']],
      status=CertificateStatus.ACTIVE,
      fullchain_path=f'{self.settings.LETSENCRYPT_DIR}/live/{cert_name}/fullchain.pem',
      private_key_path=f'{self.settings.LETSENCRYPT_DIR}/live/{cert_name}/privkey.pem')

  def available_path(self, config_type, filename):
    key = 'NGINX_STREAMS_AVAILABLE' if config_type == 'stream' else 'NGINX_SITES_AVAILABLE'
    return str(Path(getattr(self.settings, key)) / filename)

  def server_blocks(self, text):
    sanitized = self.strip_comments(text)
    blocks = []
    for match in re.finditer(r'\bserver\s*\{', sanitized):
      start, depth, index = match.end(), 1, match.end()
      while index < len(sanitized) and depth:
        if sanitized[index] == '{':
          depth += 1
        elif sanitized[index] == '}':
          depth -= 1
        index += 1
      if depth == 0:
        blocks.append(sanitized[start:index - 1])
    return blocks

  def directive(self, block, name):
    match = re.search(rf'\b{re.escape(name)}\s+([^;{{}}]+);', block)
    return match.group(1).strip() if match else None

  def server_names(self, block):
    value = self.directive(block, 'server_name')
    return value.split() if value else []

  def block_has_ssl(self, block):
    return bool(re.search(r'\blisten\s+[^;]*\bssl\b[^;]*;', block))

  def cert_name(self, block):
    certificate = self.directive(block, 'ssl_certificate')
    match = re.search(r'/live/([^/]+)/fullchain\.pem$', certificate or '')
    return match.group(1) if match else None

  def strip_comments(self, text):
    return re.sub(r'#.*$', '', text, flags=re.MULTILINE)
