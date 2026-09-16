import asyncio
import logging
import shlex
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from mako.template import Template
from .file_transaction import FileTransaction

logger = logging.getLogger(__name__)


class NginxOperationError(RuntimeError):
  def __init__(self, message, *, rollback_failed=False):
    super().__init__(message)
    self.rollback_failed = rollback_failed


class Nginx:
  def __init__(self) -> None:
    self.templates_dir = Path(__file__).resolve().parents[1] / 'templates'

  def render_config(self, **kwargs) -> str:
    proxy_type = kwargs.get('type', kwargs.get('proxy_type', 'hostname'))
    if self._value(proxy_type) in ('port_proxy', 'stream'):
      return self.render_stream_config(**kwargs)
    return self.render_http_config(**kwargs)

  def render_http_config(self, **kwargs) -> str:
    name = kwargs.get('name', kwargs.get('domain'))
    server_names = kwargs.get('server_names') or [name]
    return self._render(
      'nginx_http_config.mako',
      server_names=self._server_names(server_names),
      upstream_host=self._upstream_host(kwargs['upstream_host']),
      upstream_port=kwargs['upstream_port'],
      upstream_scheme=self._value(kwargs.get('upstream_scheme', kwargs.get('scheme', 'http'))),
      ssl_enabled=bool(kwargs.get('ssl_enabled', False)),
      cert_name=kwargs.get('cert_name', name),
      certbot_webroot=self._path(kwargs.get('certbot_webroot', '/var/www/certbot')),
      letsencrypt_dir=self._path(kwargs.get('letsencrypt_dir', '/etc/letsencrypt')),
      client_max_body_size=kwargs.get('client_max_body_size', '50m'),
    )

  def render_stream_config(self, **kwargs) -> str:
    return self._render(
      'nginx_stream_config.mako',
      listen_port=kwargs['listen_port'],
      stream_protocol=self._value(kwargs.get('stream_protocol', 'tcp')),
      upstream_host=self._upstream_host(kwargs['upstream_host']),
      upstream_port=kwargs['upstream_port'],
      proxy_connect_timeout=kwargs.get('proxy_connect_timeout', '10s'),
      proxy_timeout=kwargs.get('proxy_timeout', '1h'),
    )

  def _render(self, template: str, **kwargs) -> str:
    text = Template(filename=str(self.templates_dir / template)).render(**kwargs)
    return text.strip() + '\n'

  def _server_names(self, value: Any) -> list[str]:
    if isinstance(value, str):
      return [a for a in value.replace(',', ' ').split() if a]
    return [self._server_name(a) for a in value]

  def _server_name(self, value: Any) -> str:
    if isinstance(value, dict):
      return str(value['name'])
    if hasattr(value, 'name'):
      return str(value.name)
    return str(value)

  def _value(self, value: Any) -> Any:
    return value.value if hasattr(value, 'value') else value

  def _path(self, value: Any) -> str:
    return str(value).replace('\\', '/')


  def _upstream_host(self, value):
    host = str(value)
    return f'[{host}]' if ':' in host and not host.startswith('[') else host

  @asynccontextmanager
  async def transaction(self, settings, paths):
    files = FileTransaction(settings, paths)
    files.capture()
    try:
      yield files
    except BaseException as original:
      async def recover():
        files.rollback()
        await self.validate_reload(settings)
      try:
        await asyncio.shield(recover())
      except Exception as error:
        raise NginxOperationError(f'{original}; rollback_failed: {error}', rollback_failed=True) from original
      if isinstance(original, OSError):
        raise NginxOperationError(str(original)) from original
      raise
    else:
      # The database has already committed. A manifest failure must not undo it.
      try:
        files.commit()
      except OSError:
        logger.exception('Could not mark nginx transaction committed: %s', files.directory)

  async def validate_reload(self, settings):
    self.require_success(await self.test(settings), 'nginx_test_failed')
    result = await self.reload(settings)
    if result is not None:
      self.require_success(result, 'nginx_reload_failed')

  def require_success(self, result, fallback):
    if not result['success']:
      raise NginxOperationError(result.get('stderr') or result.get('stdout') or fallback)

  async def test(self, settings):
    return await self.run_command(*self.command_args(settings.NGINX_TEST_COMMAND),
                    timeout=settings.NGINX_COMMAND_TIMEOUT)

  async def reload(self, settings):
    if not settings.NGINX_RELOAD_COMMAND:
      return None
    return await self.run_command(*self.command_args(settings.NGINX_RELOAD_COMMAND),
                    timeout=settings.NGINX_COMMAND_TIMEOUT)

  async def run_command(self, *args, timeout=30):
    logger.info('Running command: %s', ' '.join(args))
    process = None
    try:
      process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
      stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
      return dict(success=process.returncode == 0, command=list(args),
            stdout=stdout.decode('utf-8', errors='replace').strip(),
            stderr=stderr.decode('utf-8', errors='replace').strip(),
            returncode=process.returncode)
    except asyncio.TimeoutError:
      process.kill()
      await process.communicate()
      return dict(success=False, command=list(args), stdout='',
            stderr='command_timeout', returncode=-1)
    except asyncio.CancelledError:
      if process is not None and process.returncode is None:
        process.kill()
        await process.communicate()
      raise
    except OSError as error:
      return dict(success=False, command=list(args), stdout='', stderr=str(error), returncode=-1)

  def command_args(self, command):
    args = shlex.split(command) if isinstance(command, str) else list(command)
    if not args:
      raise ValueError('nginx_test_command_required')
    return args

  async def issue_certificate(self, domain_name, server_names, settings):
    for key in ('CERTBOT_WEBROOT', 'LETSENCRYPT_DIR', 'CERTBOT_WORK_DIR', 'CERTBOT_LOGS_DIR'):
      Path(getattr(settings, key)).mkdir(parents=True, exist_ok=True)
    args = [
      settings.CERTBOT_BIN, 'certonly', '--webroot', '-w', settings.CERTBOT_WEBROOT,
      '--non-interactive', '--agree-tos', '--config-dir', settings.LETSENCRYPT_DIR,
      '--work-dir', settings.CERTBOT_WORK_DIR, '--logs-dir', settings.CERTBOT_LOGS_DIR,
      '--cert-name', domain_name,
    ]
    if settings.CERTBOT_STAGING:
      args.append('--staging')
    if settings.CERTBOT_EMAIL:
      args.extend(['--email', settings.CERTBOT_EMAIL])
    else:
      args.append('--register-unsafely-without-email')
    for name in server_names:
      args.extend(['-d', name])
    return await self.run_command(*args, timeout=settings.CERTBOT_COMMAND_TIMEOUT)

  async def certificate_expiry(self, fullchain, settings):
    result = await self.run_command(settings.OPENSSL_BIN, 'x509', '-in', fullchain,
                    '-noout', '-enddate', timeout=settings.NGINX_COMMAND_TIMEOUT)
    self.require_success(result, 'certificate_metadata_failed')
    text = result['stdout'].removeprefix('notAfter=').strip()
    try:
      return datetime.strptime(text, '%b %d %H:%M:%S %Y %Z')
    except ValueError as error:
      raise NginxOperationError('certificate_metadata_invalid') from error
