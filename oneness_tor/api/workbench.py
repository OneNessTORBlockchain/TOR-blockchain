"""The app builders — project files, commands, dependencies and previews.

Two tools share one endpoint family:

* ``/api/workbench/projects`` … the DarkWeb App Builder (web) and the Rust
  Program Builder (cargo) projects,
* ``/{id}/files`` read / create / write / delete, ``/{id}/run`` and
  ``/{id}/install`` execute real commands, ``/{id}/ai`` applies a model's JSON
  file plan and ``/{id}/serve`` starts the app for the preview window.

Ownership is the account wallet that created the project (``x-wallet-session``,
earned at ``/wallet`` with username + password). Only that wallet — or the node
admin session from ``/admin`` — can write, run, install, AI-edit, serve or
deploy it. Reads stay public so the preview and the project list work in a
browser tab. No admin token is involved anywhere.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core.errors import Forbidden, ValidationError
from oneness_tor.services import wallet_access, workbench
from oneness_tor.services.auth import is_admin_request

bp = Blueprint("workbench_api", __name__, url_prefix="/api")


def envelope(data, **meta):
    body = {"ok": True, "data": data, "error": None}
    if meta:
        body["meta"] = meta
    return jsonify(body)


def _body() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValidationError("a JSON body is required")
    return payload


def _limit(default: int = 50) -> int:
    raw = request.args.get("limit", default)
    try:
        return max(1, min(200, int(raw)))
    except (TypeError, ValueError):
        return default


def _timeout(value) -> float:
    try:
        return max(1.0, min(1800.0, float(value)))
    except (TypeError, ValueError):
        return 120.0


@bp.get("/workbench/projects")
def projects_list():
    return envelope(workbench.list_projects(_limit()))


@bp.post("/workbench/projects")
def projects_create():
    """Create a project owned by the signed-in account wallet (admin may set one)."""
    payload = _body()
    held = wallet_access.session(request)
    if is_admin_request(request):
        owner = str(payload.get("owner_wallet") or payload.get("wallet") or "")
        address = str(payload.get("owner_address") or "")
    elif held:
        owner = str(held.get("wallet"))
        address = str(held.get("address") or "")
        if payload.get("owner_wallet") and str(payload["owner_wallet"]) != owner:
            raise Forbidden("a wallet session can only create its own projects")
    else:
        raise Forbidden(
            "sign in at /wallet first — your own wallet owns the projects you build",
            details={"header": wallet_access.HEADER})
    return envelope({"project": workbench.create(
        payload.get("name"), payload.get("kind") or "web",
        description=payload.get("description") or "",
        model=payload.get("model"), owner_wallet=owner,
        owner_address=address)})


@bp.get("/workbench/projects/<build_id>")
def project_get(build_id: str):
    return envelope({"project": workbench.get_project(build_id)})


@bp.delete("/workbench/projects/<build_id>")
def project_delete(build_id: str):
    _owner_or_admin(build_id)
    return envelope(workbench.delete_project(build_id))


@bp.get("/workbench/projects/<build_id>/files")
def file_read(build_id: str):
    path = str(request.args.get("path") or "").strip()
    if not path:
        raise ValidationError("path is required, e.g. ?path=index.html")
    return envelope(workbench.read_file(build_id, path))


@bp.post("/workbench/projects/<build_id>/files")
def file_write(build_id: str):
    _owner_or_admin(build_id)
    payload = _body()
    return envelope(workbench.write_file(
        build_id, payload.get("path"), payload.get("content"),
        mode=str(payload.get("mode") or "overwrite")))


@bp.delete("/workbench/projects/<build_id>/files")
def file_delete(build_id: str):
    _owner_or_admin(build_id)
    path = str(request.args.get("path") or "").strip()
    if not path:
        raise ValidationError("path is required, e.g. ?path=notes/todo.md")
    return envelope(workbench.delete_file(build_id, path))


@bp.post("/workbench/projects/<build_id>/run")
def project_run(build_id: str):
    _owner_or_admin(build_id)
    payload = _body()
    return envelope(workbench.run_command(
        build_id, payload.get("command"),
        timeout=_timeout(payload.get("timeout"))))


@bp.post("/workbench/projects/<build_id>/deploy")
def project_deploy(build_id: str):
    """Deploy for real: web → Tor onion URL, rust → compiled + owner-only program."""
    payload = _body()
    row = workbench.PROJECTS.get(build_id)
    held = wallet_access.session(request)
    if is_admin_request(request):
        owner = str(payload.get("owner_wallet") or payload.get("wallet")
                    or row.get("owner_wallet") or "founder")
    else:
        if not held:
            raise Forbidden(
                "deploying needs the owner's wallet session — sign in at /wallet",
                details={"header": wallet_access.HEADER})
        owner = str(held.get("wallet"))
        if payload.get("owner_wallet") and str(payload["owner_wallet"]) != owner:
            raise Forbidden("a wallet session can only deploy its own program")
    return envelope(workbench.deploy(build_id, wallet=owner, owner_wallet=owner))


@bp.get("/workbench/projects/<build_id>/deployment")
def project_deployment(build_id: str):
    row = workbench.get_project(build_id, with_tree=False)
    return envelope({"project": row.get("id"), "kind": row.get("kind"),
                     "owner_wallet": row.get("owner_wallet"),
                     "owner_address": row.get("owner_address"),
                     "deployment": row.get("deployment"),
                     "deployed_at": row.get("deployed_at")})


def _owner_or_admin(build_id: str) -> str:
    """Admin, or the wallet session recorded as the project's owner."""
    row = workbench.PROJECTS.get(build_id)
    if is_admin_request(request):
        return str(row.get("owner_wallet") or "founder")
    owner = str(row.get("owner_wallet") or "")
    if owner:
        wallet_access.authorize_wallet(request, owner)
        return owner
    raise Forbidden(
        "this project has no owner yet — sign in at /wallet and create your own, or "
        "ask the node operator to deploy it once from /admin",
        details={"project": str(build_id)})


@bp.post("/workbench/projects/<build_id>/install")
def project_install(build_id: str):
    _owner_or_admin(build_id)
    payload = _body()
    return envelope(workbench.install(
        build_id, payload.get("manager") or "pip", payload.get("packages")))


@bp.post("/workbench/projects/<build_id>/ai")
def project_ai(build_id: str):
    _owner_or_admin(build_id)
    payload = _body()
    return envelope(workbench.ai_edit(
        build_id, payload.get("prompt"), model=payload.get("model"),
        history=payload.get("history"),
        apply=bool(payload.get("apply", True))))


@bp.post("/workbench/projects/<build_id>/serve")
def project_serve(build_id: str):
    _owner_or_admin(build_id)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        raise ValidationError("a JSON body is required")
    return envelope(workbench.serve(build_id, command=payload.get("command"),
                                    port=payload.get("port")))


@bp.get("/workbench/projects/<build_id>/serve")
def project_serve_status(build_id: str):
    return envelope(workbench.server_status(build_id))


@bp.post("/workbench/projects/<build_id>/serve/stop")
def project_serve_stop(build_id: str):
    _owner_or_admin(build_id)
    return envelope(workbench.stop_server(build_id))
