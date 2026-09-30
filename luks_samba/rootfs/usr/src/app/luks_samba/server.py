"""aiohttp server for the Ingress panel."""

from __future__ import annotations

import logging
import os
import posixpath
from pathlib import Path

from aiohttp import web

from .manager import Manager, UserError
from .state import SHARE_ROOTS, ValidationError, is_under

_LOGGER = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
# The Supervisor's Ingress proxy. With host networking the port is also
# reachable from the LAN, so everything else is refused.
INGRESS_IPS = {"172.30.32.2"}
ALLOW_ANY = os.environ.get("LS_ALLOW_ANY_CLIENT") == "1"  # local development only


@web.middleware
async def guard(request: web.Request, handler):
    if not ALLOW_ANY and request.remote not in INGRESS_IPS:
        return web.Response(status=403, text="Forbidden - use the Home Assistant panel")
    try:
        return await handler(request)
    except (UserError, ValidationError) as err:
        body = {"error": str(err)}
        if getattr(err, "details", None):
            body["details"] = err.details
        return web.json_response(body, status=400)
    except web.HTTPException:
        raise
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.exception("Request failed")
        return web.json_response({"error": f"Internal error: {err}"}, status=500)


async def _json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError as err:
        raise ValidationError("Invalid JSON") from err
    if not isinstance(data, dict):
        raise ValidationError("Expected a JSON object")
    return data


def build_app(manager: Manager) -> web.Application:
    routes = web.RouteTableDef()
    def ok():
        return web.json_response({"ok": True})

    @routes.get("/")
    async def index(_):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.get("/api/status")
    async def status(_):
        return web.json_response(await manager.status())

    # volumes
    @routes.post("/api/volumes")
    async def add_volume(req):
        return web.json_response(await manager.add_volume(await _json(req)))

    @routes.put("/api/volumes/{id}")
    async def update_volume(req):
        await manager.update_volume(req.match_info["id"], await _json(req))
        return ok()

    @routes.delete("/api/volumes/{id}")
    async def delete_volume(req):
        await manager.delete_volume(req.match_info["id"])
        return ok()

    @routes.post("/api/volumes/{id}/unlock")
    async def unlock(req):
        data = await _json(req)
        await manager.unlock(req.match_info["id"], data.get("passphrase") or None)
        return ok()

    @routes.post("/api/volumes/{id}/lock")
    async def lock(req):
        await manager.lock_volume(req.match_info["id"])
        return ok()

    # users
    @routes.post("/api/users")
    async def add_user(req):
        await manager.add_user(await _json(req))
        return ok()

    @routes.put("/api/users/{name}")
    async def set_password(req):
        await manager.set_password(req.match_info["name"], await _json(req))
        return ok()

    @routes.delete("/api/users/{name}")
    async def delete_user(req):
        await manager.delete_user(req.match_info["name"])
        return ok()

    # shares
    @routes.post("/api/shares")
    async def add_share(req):
        return web.json_response(await manager.add_share(await _json(req)))

    @routes.put("/api/shares/{id}")
    async def update_share(req):
        await manager.update_share(req.match_info["id"], await _json(req))
        return ok()

    @routes.delete("/api/shares/{id}")
    async def delete_share(req):
        await manager.delete_share(req.match_info["id"])
        return ok()

    # settings
    @routes.put("/api/settings")
    async def settings(req):
        await manager.update_settings(await _json(req))
        return ok()

    @routes.get("/api/browse")
    async def browse(req):
        path = req.query.get("path", "")
        if not path:
            return web.json_response({"path": "", "parent": None,
                                      "dirs": [r for r in SHARE_ROOTS if os.path.isdir(r)]})
        path = os.path.realpath(posixpath.normpath(path))
        if not any(is_under(path, r) for r in SHARE_ROOTS) or not os.path.isdir(path):
            raise UserError("Folder not available")
        try:
            dirs = sorted(
                (e.path for e in os.scandir(path)
                 if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
                key=str.lower,
            )
        except OSError as err:
            raise UserError(f"Cannot list folder: {err.strerror}") from err
        parent = posixpath.dirname(path) if path not in SHARE_ROOTS else ""
        return web.json_response({"path": path, "parent": parent, "dirs": dirs})

    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC, append_version=True)
    return app
