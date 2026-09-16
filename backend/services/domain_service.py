from backend.utils.time import utcnow
import asyncio
import hashlib
import re
from datetime import datetime
from functools import wraps

from sqlalchemy import select
from backend.config import Settings
from backend.functions.normalization import normalize_payload
from backend.models import Domain, DomainCertificate, DomainDeployment, DomainRoute, DomainServerName
from backend.models.certificates import CertificateStatus
from backend.models.deployments import DeploymentStatus
from backend.models.domains import DomainStatus, DomainType
from backend.models.routes import StreamProtocol
from .nginx import Nginx, NginxOperationError


class DomainNotFound(Exception):
  pass


class ConflictError(ValueError):
  pass


def serialized(method):
  @wraps(method)
  async def wrapper(self, session, *args, **kwargs):
    async with self.lock:
      try:
        return await method(self, session, *args, **kwargs)
      except BaseException:
        await session.rollback()
        raise
  return wrapper


class DomainService:
  def __init__(self, *, settings=None, nginx=None):
    self.settings = settings or Settings()
    self.nginx = nginx or Nginx()
    self.lock = asyncio.Lock()

  async def find(self, session, **kwargs):
    domain = await Domain.first(session, **kwargs)
    if domain is None:
      raise DomainNotFound()
    return domain

  async def all(self, session, **kwargs):
    return await Domain.get_json(session, **kwargs)

  async def check_conflicts(self, session, payload, domain_id=None):
    names = [a['name'] for a in payload['server_names']]
    query = select(DomainServerName).where(DomainServerName.name.in_(names))
    if domain_id is not None:
      query = query.where(DomainServerName.domain_id != domain_id)
    if (await session.execute(query)).first():
      raise ConflictError('server_name_already_registered')
    if payload['domain']['type'] == DomainType.PORT_PROXY:
      route = payload['route']
      if route['stream_protocol'] == StreamProtocol.TCP and route['listen_port'] in (80, 443, self.settings.APP_PORT):
        raise ValueError('listen_port_reserved')
      query = select(DomainRoute).join(Domain).where(
        Domain.type == DomainType.PORT_PROXY, DomainRoute.listen_port == route['listen_port'],
        DomainRoute.stream_protocol == route['stream_protocol'])
      if domain_id is not None:
        query = query.where(Domain.id != domain_id)
      if (await session.execute(query)).first():
        raise ConflictError('listen_port_already_registered')
    filename = self.domain_to_filename(payload['domain']['name'])
    available, enabled = self.nginx_paths(payload['domain']['type'], filename)
    query = select(DomainDeployment.id).where(
      (DomainDeployment.sites_available_path == available) |
      (DomainDeployment.sites_enabled_path == enabled))
    if domain_id is not None:
      query = query.where(DomainDeployment.domain_id != domain_id)
    if (await session.execute(query)).first():
      raise ConflictError('deployment_path_already_registered')

  @serialized
  async def create_domain(self, session, data):
    payload = normalize_payload(data)
    await self.check_conflicts(session, payload)
    domain = Domain(**payload['domain'])
    domain.route = DomainRoute(**payload['route'])
    domain.server_names = [DomainServerName(**a) for a in payload['server_names']]
    domain.certificates = []
    domain.deployments = [self.build_deployment(payload)]
    session.add(domain)
    await session.commit()
    return domain.json

  @serialized
  async def update_domain(self, session, data):
    domain = await self.find(session, id=data.get('id'))
    payload = normalize_payload(data, existing=domain.json)
    await self.check_conflicts(session, payload, domain.id)
    active = domain.applied_deployment
    preserve_tls = bool(active and active.ssl_enabled and payload['domain']['type'] == DomainType.HOSTNAME)
    for key, value in payload['domain'].items():
      setattr(domain, key, value)
    if domain.route:
      for key, value in payload['route'].items():
        setattr(domain.route, key, value)
    else:
      domain.route = DomainRoute(**payload['route'])
    self.sync_server_names(domain, payload['server_names'])
    domain.deployments.append(self.build_deployment(
      payload, ssl_enabled=preserve_tls, cert_name=active.cert_name if preserve_tls else None))
    await session.commit()
    return domain.json

  def paths(self, domain, extra=()):
    return list(dict.fromkeys(
      path for row in [*domain.deployments, *extra]
      for path in (row.sites_available_path, row.sites_enabled_path)))

  async def check_path_ownership(self, session, domain, paths):
    query = select(DomainDeployment.id).where(
      DomainDeployment.domain_id != domain.id,
      (DomainDeployment.sites_available_path.in_(paths) | DomainDeployment.sites_enabled_path.in_(paths)))
    if (await session.execute(query)).first():
      raise ConflictError('deployment_path_owned_by_another_domain')

  def remove_obsolete(self, files, domain, keep=None):
    keep_paths = {keep.sites_available_path, keep.sites_enabled_path} if keep else set()
    for path in self.paths(domain):
      if path not in keep_paths:
        files.remove(path)

  def mark_applied(self, domain, deployment):
    for row in domain.deployments:
      if row != deployment and row.status == DeploymentStatus.APPLIED:
        row.status = DeploymentStatus.SUPERSEDED
    deployment.status = DeploymentStatus.APPLIED
    deployment.applied_at = utcnow()
    deployment.last_error = None
    domain.enabled = True
    domain.status = DomainStatus.ACTIVE

  def certificate_covers(self, domain, deployment):
    if not deployment.ssl_enabled:
      return True
    names = set(self.domain_server_names(domain))
    for cert in domain.certificates:
      if cert.status != CertificateStatus.ACTIVE or cert.cert_name != deployment.cert_name:
        continue
      if cert.expires_at and cert.expires_at <= utcnow():
        continue
      covered = set(cert.server_names or [])
      if not covered and domain.applied_deployment:
        for directive in re.findall(r'\bserver_name\s+([^;]+);', domain.applied_deployment.config_text):
          covered.update(directive.split())
      if names <= covered:
        return True
    return False

  async def record_apply_failure(self, session, domain_id, deployment_id, error):
    await session.rollback()
    domain = await self.find(session, id=domain_id)
    target = next((a for a in domain.deployments if a.id == deployment_id), None)
    if target:
      if target.status != DeploymentStatus.APPLIED:
        target.status = DeploymentStatus.ERROR
      target.last_error = str(error)
    if error.rollback_failed or not domain.enabled:
      domain.status = DomainStatus.ERROR
    await session.commit()

  @serialized
  async def apply_domain(self, session, domain_id):
    domain = await self.find(session, id=domain_id)
    deployment = domain.latest_deployment
    if deployment is None:
      deployment = self.build_deployment(self.payload_from_domain(domain))
      domain.deployments.append(deployment)
      await session.flush()
    if not self.certificate_covers(domain, deployment):
      raise ValueError('certificate_does_not_cover_server_names; request_certificate_first')
    paths = self.paths(domain)
    await self.check_path_ownership(session, domain, paths)
    deployment_id = deployment.id
    try:
      async with self.nginx.transaction(self.settings, paths) as files:
        files.write_deployment(deployment)
        self.remove_obsolete(files, domain, keep=deployment)
        await self.nginx.validate_reload(self.settings)
        self.mark_applied(domain, deployment)
        await session.commit()
    except (NginxOperationError, OSError) as error:
      error = error if isinstance(error, NginxOperationError) else NginxOperationError(str(error))
      await self.record_apply_failure(session, domain_id, deployment_id, error)
      raise error
    return domain.json

  @serialized
  async def disable_domain(self, session, domain_id):
    domain = await self.find(session, id=domain_id)
    paths = self.paths(domain)
    await self.check_path_ownership(session, domain, paths)
    async with self.nginx.transaction(self.settings, paths) as files:
      for row in domain.deployments:
        files.remove(row.sites_enabled_path)
      await self.nginx.validate_reload(self.settings)
      domain.enabled = False
      domain.status = DomainStatus.DISABLED
      await session.commit()
    return domain.json

  @serialized
  async def delete(self, session, domain_id):
    domain = await self.find(session, id=domain_id)
    data = domain.json
    paths = self.paths(domain)
    await self.check_path_ownership(session, domain, paths)
    async with self.nginx.transaction(self.settings, paths) as files:
      self.remove_obsolete(files, domain)
      await self.nginx.validate_reload(self.settings)
      await session.delete(domain)
      await session.commit()
    return data

  @serialized
  async def issue_certificate(self, session, domain_id):
    domain = await self.find(session, id=domain_id)
    if domain.type != DomainType.HOSTNAME:
      raise ValueError('ssl_only_supported_for_hostname')
    names = self.domain_server_names(domain)
    payload = self.payload_from_domain(domain)
    active = domain.applied_deployment
    challenge = self.build_deployment(
      payload, ssl_enabled=bool(active and active.ssl_enabled),
      cert_name=active.cert_name if active else None)
    tls = self.build_deployment(payload, ssl_enabled=True, cert_name=domain.name)
    cert = DomainCertificate(
      provider='certbot', cert_name=domain.name, server_names=names,
      status=CertificateStatus.PENDING, last_renewal_attempt_at=utcnow())
    paths = self.paths(domain, (challenge, tls))
    await self.check_path_ownership(session, domain, paths)
    try:
      async with self.nginx.transaction(self.settings, paths) as files:
        files.write_deployment(challenge)
        self.remove_obsolete(files, domain, keep=challenge)
        await self.nginx.validate_reload(self.settings)
        result = await self.nginx.issue_certificate(domain.name, names, self.settings)
        self.nginx.require_success(result, 'certbot_failed')
        cert.fullchain_path = f'{self.settings.LETSENCRYPT_DIR}/live/{domain.name}/fullchain.pem'
        cert.private_key_path = f'{self.settings.LETSENCRYPT_DIR}/live/{domain.name}/privkey.pem'
        cert.expires_at = await self.nginx.certificate_expiry(cert.fullchain_path, self.settings)
        if cert.expires_at <= utcnow():
          raise NginxOperationError('issued_certificate_expired')
        files.write_deployment(tls)
        await self.nginx.validate_reload(self.settings)
        cert.status = CertificateStatus.ACTIVE
        cert.issued_at = utcnow()
        domain.certificates.append(cert)
        # Other cert names may still be useful after changing aliases.
        for previous in domain.certificates:
          if previous is not cert and previous.cert_name == cert.cert_name and previous.status == CertificateStatus.ACTIVE:
            previous.status = CertificateStatus.REVOKED
        domain.deployments.append(tls)
        self.mark_applied(domain, tls)
        await session.commit()
    except (NginxOperationError, OSError) as error:
      error = error if isinstance(error, NginxOperationError) else NginxOperationError(str(error))
      await session.rollback()
      domain = await self.find(session, id=domain_id)
      domain.certificates.append(DomainCertificate(
        provider='certbot', cert_name=domain.name, server_names=names,
        status=CertificateStatus.ERROR, last_error=str(error),
        last_renewal_attempt_at=utcnow()))
      if error.rollback_failed or not domain.enabled:
        domain.status = DomainStatus.ERROR
      await session.commit()
      raise error
    return domain.json

  def build_deployment(self, payload, ssl_enabled=False, cert_name=None):
    domain, route = payload['domain'], payload['route']
    filename = self.domain_to_filename(domain['name'])
    available, enabled = self.nginx_paths(domain['type'], filename)
    return DomainDeployment(
      status=DeploymentStatus.DRAFT, nginx_filename=filename,
      sites_available_path=available, sites_enabled_path=enabled,
      ssl_enabled=ssl_enabled, cert_name=(cert_name or domain['name']) if ssl_enabled else None,
      config_text=self.nginx.render_config(
        **domain, **route, server_names=payload.get('server_names', []),
        ssl_enabled=ssl_enabled, cert_name=cert_name or domain['name'],
        certbot_webroot=self.settings.CERTBOT_WEBROOT, letsencrypt_dir=self.settings.LETSENCRYPT_DIR))

  def payload_from_domain(self, domain):
    return dict(domain=dict(name=domain.name, type=domain.type, enabled=domain.enabled, status=domain.status),
          server_names=[a.json for a in domain.server_names], route=domain.route.json)

  def latest_deployment(self, domain):
    return domain.latest_deployment

  def domain_server_names(self, domain):
    return [domain.name, *[a.name for a in domain.server_names if a.name != domain.name]]

  def domain_to_filename(self, name):
    if re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,248}', name):
      return f'{name}.conf'
    slug = re.sub(r'[^a-z0-9.-]', '-', name)[:180]
    return f'{slug}-{hashlib.sha256(name.encode()).hexdigest()[:16]}.conf'

  def sync_server_names(self, domain, payload_names):
    existing = {row.name: row for row in domain.server_names}
    rows = []
    for item in payload_names:
      row = existing.get(item['name']) or DomainServerName(**item)
      row.is_primary = item['is_primary']
      rows.append(row)
    domain.server_names = rows

  def nginx_paths(self, domain_type, filename):
    if domain_type == DomainType.PORT_PROXY:
      return (f'{self.settings.NGINX_STREAMS_AVAILABLE}/{filename}',
          f'{self.settings.NGINX_STREAMS_ENABLED}/{filename}')
    return (f'{self.settings.NGINX_SITES_AVAILABLE}/{filename}',
        f'{self.settings.NGINX_SITES_ENABLED}/{filename}')
