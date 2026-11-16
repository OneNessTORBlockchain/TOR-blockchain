"""The chain's record of which VM serves which wallet, for which product.

The three paid ANONYMOUS products each run untrusted work in a VM per wallet.
That sentence has three parts, and until this module existed only two of them
were facts the chain could see: *wallet* (the four-header proof) and *membership*
(the five free messages and the paid period). The third -- **which VM** -- was
missing, so one wallet could be served by two VMs and two wallets could be
dropped into one, invisible to everybody.

So this is the missing link, and it is deliberately small:

    POST /membership/<address>/sessions                  open (or reuse) a lease
    GET  /membership/<address>/sessions                  this wallet's leases
    POST /membership/<address>/sessions/<id>/heartbeat   keep it alive
    POST /membership/<address>/sessions/<id>/close       give it back

A lease is one row::

    {id, wallet, product, tenant, mode, state, ttl_seconds,
     created_unix, last_seen_unix, expires_unix, closed_unix, reason}

``tenant`` is the VM's name -- products run their container after it, so a VM
cannot exist without a lease, and a second VM for the same wallet in the same
product cannot exist either (the second call *reuses* the row). ``mode`` is how
the wallet earned it: ``member`` (a paid period) or ``free`` (one of the five).
There is no third way in: without either, opening a session is a 402.

Four invariants, each one a fraud the products could otherwise commit:

1. one **live** lease per ``(wallet, product)``; ``replace: true`` retires the old
   row (``state: "replaced"``) and its id is a 409 from then on, so the old VM
   loses its lease the moment the new one takes it;
2. a ``tenant`` belongs to **one live lease across all products** -- two wallets
   cannot share a VM, and a webapp VM cannot be re-used as an API VM;
3. ``product`` is one of :data:`SCOPES`, so a typo cannot mint a private
   namespace (which is how a wallet would otherwise be handed a second VM);
4. a lease is the wallet's: the caller proved that wallet to the node
   (:mod:`oneness_tor.services.wallet_proof`) before it reached this module.

What this module does **not** do: start, stop or delete a VM. The chain reports
that a lease ended; stopping the container is the product's job. It also never
publishes a lease -- ``GET /membership/<address>``, the public answer, carries no
session data, because "which wallet has a VM alive right now" is not a fact an
anonymity product should hand to a stranger with a browser.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from core.errors import (AppError, Forbidden, MembershipRequired, NotFound,
                        ValidationError)
from oneness_tor.services import membership
from oneness_tor.services.store import data_dir, read_json, write_json

#: The document name every answer of this module carries.
STANDARD = "oneness-tor/wallet-session/1"
#: The only product namespaces that may hold a lease. A new product is a code
#: change on purpose: a typo must not be able to mint a second, empty namespace.
SCOPES = ("webapp", "api", "desktop")
#: A lease the product never renews dies in an hour (the products heartbeat).
DEFAULT_TTL = 3600
#: Nobody needs a VM lease longer than a day; a longer one is a leak, not a wish.
MAX_TTL = 24 * 3600
#: Closed leases are kept this long for the audit trail, then pruned.
PRUNE_SECONDS = 7 * 86_400

LOCK = threading.RLock()
#: ``TOR1`` + the base32 digest (lowercase, ``=`` stripped) + ``onion`` -- the same
#: shape :func:`oneness_tor.services.wallet_access.address_for_pubkey` derives.
_ADDRESS = re.compile(r"^TOR1[a-z0-9]{16,90}onion$")
#: Docker-safe container names: the tenant is handed to ``docker run --name``.
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


def _iso(stamp: int | float) -> str:
    return datetime.fromtimestamp(int(stamp), tz=timezone.utc).isoformat(
        timespec="seconds")


def sessions_path() -> Path:
    """Where the leases live: ``data/membership/sessions.json``."""
    return data_dir("membership") / "sessions.json"


def _book() -> dict[str, dict]:
    held = read_json(sessions_path(), {}) or {}
    rows = held.get("sessions") if isinstance(held, dict) else None
    return dict(rows) if isinstance(rows, dict) else {}


def _write(rows: dict[str, dict], now: int) -> None:
    """Persist the leases, dropping closed ones older than :data:`PRUNE_SECONDS`.

    Pruning happens on write only: a read must never rewrite the file (two
    readers would race each other for nothing).
    """
    keep = {}
    for key, row in rows.items():
        ended = int(row.get("closed_unix") or 0) or int(row.get("expires_unix") or 0)
        if str(row.get("state")) != "live" and ended and ended + PRUNE_SECONDS < now:
            continue
        keep[str(key)] = row
    write_json(sessions_path(), {"standard": STANDARD, "sessions": keep,
                                 "scopes": list(SCOPES), "updated_at": _iso(now)})


def _ttl(seconds: object) -> int:
    """Clamp a requested lease to ``[60, MAX_TTL]``; 0 means the default."""
    try:
        wanted = int(seconds or 0)
    except (TypeError, ValueError):
        raise ValidationError("ttl must be whole seconds",
                              details={"received": str(seconds)[:40]})
    if wanted <= 0:
        try:
            wanted = int(settings.get("membership.session_ttl") or DEFAULT_TTL)
        except (TypeError, ValueError):
            wanted = DEFAULT_TTL
    return max(60, min(MAX_TTL, wanted))


def _wallet(value: object) -> str:
    wanted = str(value or "").strip()
    if not wanted:
        raise ValidationError("a session needs the wallet's TOR1...onion address")
    if not _ADDRESS.match(wanted):
        raise ValidationError(
            "that is not a TOR1...onion wallet address",
            details={"received": wanted[:80], "expected": _ADDRESS.pattern})
    return wanted


def _product(value: object) -> str:
    wanted = str(value or "").strip().lower()
    if wanted not in SCOPES:
        raise ValidationError(
            "the product must be one of " + ", ".join(SCOPES)
            + " - a namespace of its own would be a second free VM",
            details={"received": str(value or "")[:40], "scopes": list(SCOPES)})
    return wanted


def _tenant(value: object, product: str, identifier: str) -> str:
    named = str(value or "").strip()
    if not named:
        return f"{product}-{identifier}"        # unique by construction
    if not _TENANT.match(named):
        raise ValidationError(
            f"tenant must match {_TENANT.pattern} (it becomes a container name)",
            details={"received": named[:60]})
    return named


def view(row: dict, now: int | None = None) -> dict:
    """One lease as a product reads it; ``expired`` is derived, never stored."""
    moment = int(now if now is not None else time.time())
    state = str(row.get("state") or "live")
    expires = int(row.get("expires_unix") or 0)
    if state == "live" and expires and expires <= moment:
        state = "expired"
    return {"standard": STANDARD, "id": str(row.get("id") or ""),
            "wallet": str(row.get("wallet") or ""),
            "product": str(row.get("product") or ""),
            "tenant": str(row.get("tenant") or ""),
            "mode": str(row.get("mode") or ""), "state": state,
            "live": state == "live",
            "ttl_seconds": int(row.get("ttl_seconds") or 0),
            "created_at": _iso(int(row.get("created_unix") or 0)),
            "created_unix": int(row.get("created_unix") or 0),
            "last_seen_unix": int(row.get("last_seen_unix") or 0),
            "expires_at": _iso(expires) if expires else None,
            "expires_unix": expires,
            "seconds_left": max(0, expires - moment),
            "closed_unix": row.get("closed_unix"),
            "reason": str(row.get("reason") or "")}


def _live(row: dict, now: int) -> bool:
    return bool(view(row, now)["live"])


# -------------------------------------------------- the two doors, in one place
def facts(address: str, *, now: int) -> dict:
    """The membership facts a lease is granted on (the same answer a product reads).

    ``identity`` is the public ``GET /membership/<address>`` payload, so the
    session service can never disagree with what the products are told.
    """
    state = membership.identity(address, now=now)
    free_left = int(state.get("free_messages_left") or 0)
    return {"active": bool(state.get("active")),
            "until": state.get("until"),
            "expires_at": int(state.get("expires_at") or 0),
            "plan": state.get("plan"),
            "free_messages_left": free_left,
            "free_messages_limit": int(state.get("free_messages_limit") or 0),
            "free_messages_used": int(state.get("free_messages_used") or 0),
            "price_usd": state.get("price_usd")}


def payment_needed(address: str, known: dict) -> MembershipRequired:
    """The 402 a product shows instead of a VM: five messages, or a subscription.

    The words and the details are the ones :func:`membership.use_free` raises, so
    a visitor sees one paywall sentence whether the wall was hit on a message or
    on a VM.
    """
    return MembershipRequired(
        "this wallet has no active membership and no free message left - a VM "
        f"session needs one or the other (${known['price_usd']:.2f} per "
        f"{membership.period_days()} days in TOR)",
        details={"address": address, "free_messages_left": 0,
                 "free_messages_limit": known.get("free_messages_limit"),
                 "price_usd": known["price_usd"],
                 "days": membership.period_days(),
                 "subscribe": f"POST /membership/{address}/invoices",
                 "settle": f"POST /membership/{address}/settle",
                 "sessions": f"POST /membership/{address}/sessions"})


# -------------------------------------------------------------- open a lease
def open_(wallet: str, product: str, *, tenant: str = "", ttl: object = 0,
          replace: bool = False, note: str = "",
          now: int | None = None) -> dict:
    """Grant this wallet a VM lease in this product, or hand back the live one.

    Reuse is the point: a second tab, a reloaded page or a second process asking
    for the same wallet in the same product gets the **same** lease and therefore
    the same VM -- it does not get a second one. ``replace=True`` is the explicit
    "kill the old VM" (sign-out on another device): the old row is retired to
    ``state: "replaced"`` and its id answers 409 forever after.

    Naming a ``tenant`` that another wallet holds is a **403** before anything
    else is considered (so it can never be reused by luck), and naming a tenant
    that differs from the wallet's own live lease is a **409** (ask with
    ``replace``) rather than a silent substitution.

    402 when the wallet has neither a paid period nor a free message left: the
    five messages and the subscription are the only two doors to a VM.
    """
    wanted = _wallet(wallet)
    scope = _product(product)
    moment = int(now if now is not None else time.time())
    ttl_seconds = _ttl(ttl)
    named = str(tenant or "").strip()
    if named:
        _tenant(named, scope, "")        # shape first: it becomes a container name
    with LOCK:
        rows = _book()
        mine = [row for row in rows.values()
                if str(row.get("wallet")) == wanted
                and str(row.get("product")) == scope and _live(row, moment)]

        # 1. a named VM belongs to whoever holds it -- checked before any reuse, so
        #    "give me A's machine" is always refused, even for a wallet that is
        #    already holding one of its own.
        if named:
            for other in rows.values():
                if (str(other.get("tenant") or "").lower() == named.lower()
                        and _live(other, moment)
                        and str(other.get("wallet")) != wanted):
                    raise Forbidden(
                        "that VM session already serves another wallet - one VM, "
                        "one wallet, and a webapp VM is never an API VM",
                        details={"tenant": named, "held_by": other.get("wallet"),
                                 "held_product": other.get("product"),
                                 "asked_by": wanted, "asked_product": scope})

        if mine and not replace:
            held_tenant = str(mine[0].get("tenant") or "")
            if named and named.lower() != held_tenant.lower():
                raise AppError(
                    f"this wallet already holds a live VM in {scope} "
                    f"({held_tenant}) - ask with replace: true to retire it",
                    code="SESSION_EXISTS", http_status=409,
                    details={"session": mine[0].get("id"), "tenant": held_tenant,
                             "asked_tenant": named,
                             "replace": True})
            return _answer(mine[0], moment, reused=True, wallet=wanted,
                           scope=scope)

        known = facts(wanted, now=moment)
        mode = ("member" if known["active"]
                else "free" if known["free_messages_left"] > 0 else "")
        if not mode:
            raise payment_needed(wanted, known)

        identifier = ""
        for _ in range(8):
            candidate = "ws_" + secrets.token_hex(8)
            if candidate not in rows:
                identifier = candidate
                break
        if not identifier:
            raise AppError("could not allocate a session id - retry",
                           code="SESSION_UNAVAILABLE", http_status=503)

        wanted_tenant = named or _tenant("", scope, identifier)
        for other in rows.values():
            if (str(other.get("tenant") or "").lower() == wanted_tenant.lower()
                    and _live(other, moment)):
                raise Forbidden(
                    "that VM session already serves another wallet - one VM, one "
                    "wallet, and a webapp VM is never an API VM",
                    details={"tenant": wanted_tenant,
                             "held_by": other.get("wallet"),
                             "held_product": other.get("product"),
                             "asked_by": wanted, "asked_product": scope})
        for row in mine:                        # replace: retire what was live
            row.update({"state": "replaced", "closed_unix": moment,
                        "reason": f"replaced by {identifier}"[:120]})
            rows[str(row["id"])] = row

        row = {"id": identifier, "wallet": wanted, "product": scope,
               "tenant": wanted_tenant, "mode": mode, "state": "live",
               "ttl_seconds": ttl_seconds, "created_unix": moment,
               "last_seen_unix": moment, "expires_unix": moment + ttl_seconds,
               "closed_unix": None, "reason": str(note or "")[:120],
               "free_messages_left": int(known["free_messages_left"])}
        rows[identifier] = row
        _write(rows, moment)
    return _answer(row, moment, reused=False, wallet=wanted, scope=scope)


def _answer(row: dict, moment: int, *, reused: bool, wallet: str,
            scope: str) -> dict:
    """What a product needs to start (or keep) the VM named ``tenant``."""
    shaped = view(row, moment)
    return {"standard": STANDARD, "session": shaped, "reused": bool(reused),
            "wallet": wallet, "product": scope, "mode": shaped["mode"],
            "tenant": shaped["tenant"],
            "free_messages_left": int(row.get("free_messages_left") or 0),
            "how": {"vm": f"run the machine named {shaped['tenant']}",
                    "keep": f"POST /membership/{wallet}/sessions/{shaped['id']}"
                            "/heartbeat",
                    "release": f"POST /membership/{wallet}/sessions/"
                               f"{shaped['id']}/close"}}


# ------------------------------------------------- and keep it, or give it back
def _find(rows: dict[str, dict], session_id: object) -> dict:
    identifier = str(session_id or "").strip()
    row = rows.get(identifier)
    if not row:
        raise NotFound(f"no VM session {identifier or '(none)'} on this node",
                       details={"session": identifier[:64]})
    return dict(row)


def _owns(row: dict, wallet: str) -> None:
    if str(row.get("wallet")) != wallet:
        raise Forbidden("that VM session belongs to another wallet",
                        details={"session": row.get("id"), "holder": row.get("wallet"),
                                 "asked_by": wallet})


def heartbeat(wallet: str, session_id: object, *, ttl: object = 0,
              now: int | None = None) -> dict:
    """Renew a lease and say whether the wallet may still be served.

    A closed or replaced lease is a **409** (open a new one); an expired one is a
    409 too, and that is the point: a chain that answered "yes" to a dead lease
    would let a product keep a VM running forever. ``allowed: False`` with
    ``reason: "payment-required"`` is the gentler answer -- the lease still
    stands, the wallet is at the paywall, and the product decides what to do.
    """
    wanted = _wallet(wallet)
    moment = int(now if now is not None else time.time())
    with LOCK:
        rows = _book()
        row = _find(rows, session_id)
        _owns(row, wanted)
        shaped = view(row, moment)
        if shaped["state"] in ("closed", "replaced"):
            raise AppError(
                f"that VM session was {shaped['state']} - open a new one",
                code="SESSION_CLOSED", http_status=409,
                details={"session": shaped["id"], "state": shaped["state"],
                         "reason": shaped["reason"],
                         "open": f"POST /membership/{wanted}/sessions"})
        if shaped["state"] == "expired":
            raise AppError(
                "that VM session expired - open a new one",
                code="SESSION_EXPIRED", http_status=409,
                details={"session": shaped["id"], "expired_at": shaped["expires_at"],
                         "open": f"POST /membership/{wanted}/sessions"})
        ttl_seconds = _ttl(ttl or row.get("ttl_seconds") or 0)
        renew_to = max(int(row.get("expires_unix") or 0), moment + ttl_seconds)
        row.update({"last_seen_unix": moment, "expires_unix": renew_to,
                    "ttl_seconds": ttl_seconds})
        rows[str(row["id"])] = row
        _write(rows, moment)

    known = facts(wanted, now=moment)
    allowed = bool(known["active"] or known["free_messages_left"] > 0)
    return {"standard": STANDARD, "session": view(row, moment),
            "extended_seconds": max(0, int(row["expires_unix"])
                                    - int(shaped["expires_unix"])),
            "mode": ("member" if known["active"] else row.get("mode") or "free"),
            "allowed": allowed,
            "reason": ("member" if known["active"]
                       else "free" if allowed else "payment-required"),
            "free_messages_left": known["free_messages_left"],
            "membership": {"active": known["active"], "until": known["until"],
                           "expires_at": known["expires_at"]},
            "price_usd": known["price_usd"]}


def close(wallet: str, session_id: object, *, reason: str = "",
          now: int | None = None) -> dict:
    """Give a lease back now (idempotent: closing a closed lease says so)."""
    wanted = _wallet(wallet)
    moment = int(now if now is not None else time.time())
    with LOCK:
        rows = _book()
        row = _find(rows, session_id)
        _owns(row, wanted)
        already = str(row.get("state")) != "live"
        if not already:
            row.update({"state": "closed", "closed_unix": moment,
                        "reason": str(reason or "closed by the product")[:120]})
            rows[str(row["id"])] = row
            _write(rows, moment)
    shaped = view(row, moment)
    return {"standard": STANDARD, "session": shaped, "closed": True,
            "already": already, "reason": shaped["reason"],
            "tenant": shaped["tenant"],
            "how": "stop the machine that was named "
                   f"{shaped['tenant']} - the chain has released it"}


def close_all(wallet: str, *, reason: str = "", now: int | None = None) -> dict:
    """Release every live lease of one wallet (used when a period ends)."""
    wanted = _wallet(wallet)
    moment = int(now if now is not None else time.time())
    freed: list[str] = []
    with LOCK:
        rows = _book()
        for row in list(rows.values()):
            if str(row.get("wallet")) != wanted or not _live(row, moment):
                continue
            row.update({"state": "closed", "closed_unix": moment,
                        "reason": str(reason or "membership ended")[:120]})
            rows[str(row["id"])] = row
            freed.append(str(row["id"]))
        if freed:
            _write(rows, moment)
    return {"standard": STANDARD, "wallet": wanted, "closed": len(freed),
            "sessions": freed, "reason": str(reason or "membership ended")[:120],
            "at": _iso(moment)}



def active(wallet: str, product: str, *, now: int | None = None) -> dict | None:
    """The one live lease this wallet holds in this product, if any."""
    wanted = _wallet(wallet)
    scope = _product(product)
    moment = int(now if now is not None else time.time())
    for row in _book().values():
        if (str(row.get("wallet")) == wanted and str(row.get("product")) == scope
                and _live(row, moment)):
            return view(row, moment)
    return None


def listing(wallet: str, *, product: str = "", now: int | None = None) -> dict:
    """This wallet's leases, newest first, grouped by product.

    ``live_by_product`` is how a product proves the separation for its own
    wallet: ``{"webapp": 1, "api": 0, "desktop": 0}`` means one webapp VM and no
    API VM -- and never one VM counted in two namespaces.
    """
    wanted = _wallet(wallet)
    scope = _product(product) if str(product or "").strip() else ""
    moment = int(now if now is not None else time.time())
    rows = [row for row in _book().values()
            if str(row.get("wallet")) == wanted
            and (not scope or str(row.get("product")) == scope)]
    shaped = sorted((view(row, moment) for row in rows),
                    key=lambda item: int(item["created_unix"]), reverse=True)
    by_product: dict[str, int] = {name: 0 for name in SCOPES}
    live_by_product: dict[str, int] = {name: 0 for name in SCOPES}
    for item in shaped:
        name = str(item["product"])
        by_product[name] = by_product.get(name, 0) + 1
        if item["live"]:
            live_by_product[name] = live_by_product.get(name, 0) + 1
    return {"standard": STANDARD, "wallet": wanted, "product": scope,
            "sessions": shaped, "count": len(shaped),
            "live": sum(1 for item in shaped if item["live"]),
            "by_product": by_product, "live_by_product": live_by_product,
            "scopes": list(SCOPES), "checked_at": _iso(moment)}


