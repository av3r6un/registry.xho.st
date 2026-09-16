import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Snapshot:
  path: Path
  backup: Path | None
  link: str | None
  mode: int


def atomic_write(path, content, mode=0o644):
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
  try:
    with temporary.open('wb') as output:
      output.write(content)
      output.flush()
      os.fsync(output.fileno())
    temporary.chmod(mode)
    temporary.replace(path)
  finally:
    temporary.unlink(missing_ok=True)


class FileTransaction:
  """Snapshots files independently, preserving symlinks without writing through them."""

  def __init__(self, settings, paths):
    directories = [
      Path(getattr(settings, key)).resolve()
      for key in ('NGINX_SITES_AVAILABLE', 'NGINX_SITES_ENABLED',
            'NGINX_STREAMS_AVAILABLE', 'NGINX_STREAMS_ENABLED')
    ]
    if len(set(directories)) != 4:
      raise ValueError('nginx_config_directories_must_be_distinct')
    self.directory = Path(settings.NGINX_BACKUP_DIR) / uuid.uuid4().hex
    self.snapshots = {}
    for value in paths:
      path = Path(value).absolute()
      if path.parent.resolve() not in directories or path.suffix != '.conf':
        raise ValueError('deployment_path_outside_config_directories')
      self.snapshots[path] = None

  def capture(self):
    self.directory.mkdir(parents=True, exist_ok=False)
    for path in self.snapshots:
      backup, link, mode = None, None, 0o644
      if path.is_symlink():
        link = os.readlink(path)
      elif path.exists():
        if not path.is_file():
          raise ValueError('deployment_path_must_be_file')
        mode = stat.S_IMODE(path.stat().st_mode)
        backup = self.directory / (hashlib.sha256(str(path).encode()).hexdigest() + '.bak')
        atomic_write(backup, path.read_bytes(), mode)
      self.snapshots[path] = Snapshot(path, backup, link, mode)
    self.write_manifest('pending')

  def write_manifest(self, state):
    records = [
      {'path': str(s.path), 'backup': str(s.backup) if s.backup else None,
       'link': s.link, 'mode': s.mode}
      for s in self.snapshots.values() if s is not None
    ]
    atomic_write(self.directory / 'manifest.json',
           json.dumps({'state': state, 'files': records}, indent=2).encode())

  def check_path(self, path):
    path = Path(path).absolute()
    if path not in self.snapshots:
      raise ValueError('file_not_in_transaction')
    return path

  def write_deployment(self, deployment):
    content = deployment.config_text.encode('utf-8')
    for value in (deployment.sites_available_path, deployment.sites_enabled_path):
      path = self.check_path(value)
      atomic_write(path, content, self.snapshots[path].mode)

  def remove(self, value):
    self.check_path(value).unlink(missing_ok=True)

  def rollback(self):
    errors = []
    for snapshot in self.snapshots.values():
      if snapshot is None:
        continue
      try:
        if snapshot.link is not None:
          snapshot.path.unlink(missing_ok=True)
          snapshot.path.symlink_to(snapshot.link)
        elif snapshot.backup is not None:
          atomic_write(snapshot.path, snapshot.backup.read_bytes(), snapshot.mode)
        else:
          snapshot.path.unlink(missing_ok=True)
      except OSError as error:
        errors.append(str(error))
    self.write_manifest('rollback_failed' if errors else 'rolled_back')
    if errors:
      raise OSError('; '.join(errors))

  def commit(self):
    self.write_manifest('committed')
