import ipaddress
import re
from typing import Any
from backend.models.routes import UpstreamScheme, StreamProtocol
from backend.models.domains import DomainStatus, DomainType

HOSTNAME_RE = re.compile(r'^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$')
PROXY_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,254}$')


def normalize_payload(initial: dict[str, Any], existing=None):
  if not isinstance(initial, dict):
    raise ValueError('payload_must_be_object')
  existing = existing or {}
  nested = nested_dict(initial.get('domain'))
  domain_type = normalize_domain_type(initial.get('type', initial.get('proxy_type', nested.get('type', existing.get('type', 'hostname')))))
  legacy_name = initial.get('domain') if isinstance(initial.get('domain'), str) else None
  name = normalize_name(initial.get('name', legacy_name or nested.get('name', existing.get('name'))), domain_type)
  route = dict(existing.get('route') or {})
  route.update(nested_dict(initial.get('route')))
  merged = {**initial, 'route': route}
  names = initial.get('server_names', existing.get('server_names'))
  return dict(
    domain=dict(name=name, type=domain_type, enabled=existing.get('enabled', False),
          status=normalize_enum(existing.get('status'), DomainStatus, DomainStatus.DRAFT)),
    server_names=normalize_server_names(names, name) if domain_type == DomainType.HOSTNAME else [],
    route=normalize_route(merged, domain_type), certificates=[], deployments=[],
  )


def normalize_domain_type(value):
  if isinstance(value, DomainType):
    return value
  if value in ('hostname', 'http'):
    return DomainType.HOSTNAME
  if value in ('port_proxy', 'stream'):
    return DomainType.PORT_PROXY
  raise ValueError('invalid_domain_type')


def normalize_name(value, domain_type):
  if domain_type == DomainType.HOSTNAME:
    return normalize_hostname(value, 'invalid_domain')
  if not isinstance(value, str) or not PROXY_NAME_RE.fullmatch(value.strip().lower()):
    raise ValueError('invalid_name')
  return value.strip().lower()


def normalize_route(initial, domain_type):
  route = nested_dict(initial.get('route'))
  raw_scheme = initial.get('upstream_scheme', initial.get('scheme', route.get('upstream_scheme')))
  protocol = initial.get('stream_protocol', route.get('stream_protocol', route.get('scheme')))
  if domain_type == DomainType.PORT_PROXY and raw_scheme in ('tcp', 'udp'):
    protocol = initial.get('stream_protocol', raw_scheme)
    raw_scheme = None
  scheme = normalize_enum(raw_scheme, UpstreamScheme, UpstreamScheme.HTTP)
  if domain_type == DomainType.PORT_PROXY:
    scheme = UpstreamScheme.STREAM
    listen_port = normalize_port(initial.get('listen_port', route.get('listen_port')), 'invalid_listen_port')
  else:
    if scheme == UpstreamScheme.STREAM:
      # An explicit type change resets the old stream scheme.
      if 'type' in initial and 'upstream_scheme' not in initial and 'scheme' not in initial:
        scheme = UpstreamScheme.HTTP
      else:
        raise ValueError('invalid_upstream_scheme')
    listen_port = None
  return dict(
    upstream_host=normalize_upstream_host(initial.get('upstream_host', route.get('upstream_host'))),
    upstream_port=normalize_port(initial.get('upstream_port', route.get('upstream_port')), 'invalid_upstream_port'),
    upstream_scheme=scheme, listen_port=listen_port,
    stream_protocol=normalize_enum(protocol, StreamProtocol, StreamProtocol.TCP),
  )


def normalize_server_names(value, domain):
  if value is None:
    names = [domain]
  elif isinstance(value, str):
    names = value.replace(',', ' ').split()
  elif isinstance(value, list):
    names = [a.get('name') if isinstance(a, dict) else a for a in value]
  else:
    raise ValueError('invalid_server_names')
  names = [normalize_hostname(a, 'invalid_server_names') for a in names]
  if len(names) != len(set(names)):
    raise ValueError('duplicate_server_names')
  names = [domain, *[a for a in names if a != domain]]
  return [dict(name=name, is_primary=index == 0) for index, name in enumerate(names)]


def normalize_hostname(value, error):
  if not isinstance(value, str):
    raise ValueError(error)
  try:
    host = value.strip().lower().encode('idna').decode('ascii')
  except UnicodeError as exc:
    raise ValueError(error) from exc
  if not HOSTNAME_RE.fullmatch(host):
    raise ValueError(error)
  return host


def normalize_upstream_host(value):
  if not isinstance(value, str):
    raise ValueError('invalid_upstream_host')
  host = value.strip()
  if host.startswith('[') and host.endswith(']'):
    host = host[1:-1]
  try:
    return str(ipaddress.ip_address(host))
  except ValueError:
    if ':' in host or re.fullmatch(r'[0-9.]+', host):
      raise ValueError('invalid_upstream_host') from None
    return normalize_hostname(host, 'invalid_upstream_host')


def normalize_port(value, error):
  if type(value) is not int or not 1 <= value <= 65535:
    raise ValueError(error)
  return value


def normalize_enum(value, enum_cls, default):
  if value is None or value == '':
    return default
  if isinstance(value, enum_cls):
    return value
  try:
    return enum_cls(value)
  except (ValueError, TypeError):
    raise ValueError(f'invalid_{camel_to_snake(enum_cls.__name__)}') from None


def nested_dict(value):
  return value if isinstance(value, dict) else {}


def camel_to_snake(value):
  return re.sub(r'(?<!^)(?=[A-Z])', '_', value).lower()
