from pathlib import Path

from aiohttp import web


def register_frontend(app, directory):
  root = Path(directory).resolve()
  if not (root / 'index.html').is_file():
    return

  async def frontend(req):
    path = req.match_info['path']
    if req.path == '/api' or req.path.startswith('/api/'):
      raise web.HTTPNotFound()
    # Resolve symlinks and reject traversal or hidden files before serving anything.
    if any(part.startswith('.') for part in path.split('/') if part):
      raise web.HTTPNotFound()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
      raise web.HTTPNotFound()
    if target.is_file():
      cache = 'no-cache' if target == root / 'index.html' else 'public, max-age=3600'
      return web.FileResponse(target, headers={'Cache-Control': cache})
    # A missing asset must stay a 404 rather than return an HTML document.
    if Path(path).suffix or path.startswith(('static/', 'img/')):
      raise web.HTTPNotFound()
    return web.FileResponse(root / 'index.html', headers={'Cache-Control': 'no-cache'})

  # Registered after API routes; GET also registers HEAD automatically.
  app.router.add_get('/{path:.*}', frontend)
