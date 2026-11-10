"""HTTP client for the Rust ledger (``rust/torchain``).

The Rust node is the only authority for balances, tokens, pools and the
permanent record. Every Python surface — pages, payments, contracts, the
Ollama tool executor — reads the chain through this client, so there is exactly
one place where the node URL, the admin token and the error envelope live.

    from core.node_client import node
    node.info()                       # {"ok": True, "data": {...}}
    node.account("TOR1…onion")
    node.submit_tx(signed)

Never raises for a chain-level error (a rejected transaction still returns the
``{ok: false, error}`` envelope); raises :class:`ChainUnavailable` only when the
node cannot be reached at all.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from core.config import settings
from core.errors import ChainUnavailable, ValidationError


class NodeClient:
    """Thin, synchronous JSON client for ``torchain serve``."""

    def __init__(self, base_url: str | None = None, *, timeout: float | None = None,
                 admin_token: str | None = None) -> None:
        self._base = (base_url or settings.node_api_url).rstrip("/")
        self._timeout = float(timeout if timeout is not None else settings.node_timeout)
        self._admin_token = admin_token

    # -- plumbing --------------------------------------------------------
    @property
    def base_url(self) -> str:
        return self._base

    def with_token(self, token: str | None) -> "NodeClient":
        """Copy of this client that presents ``token`` on admin routes."""
        return NodeClient(self._base, timeout=self._timeout, admin_token=token)

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self._base}{path if path.startswith('/') else '/' + path}"
        if query:
            clean = {key: value for key, value in query.items() if value is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        return url

    def call(self, method: str, path: str, *,
             payload: dict | Any | None = None,
             query: dict[str, Any] | None = None,
             token: str | None = None,
             timeout: float | None = None) -> dict:
        """Perform one request and always return the node envelope."""
        data = None
        headers = {"accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["content-type"] = "application/json"
        presented = token or self._admin_token
        if presented:
            headers["x-admin-token"] = presented
        request = urllib.request.Request(self._url(path, query), data=data,
                                         headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(
                    request, timeout=float(timeout or self._timeout)) as response:
                body = response.read().decode("utf-8", "replace")
                return self._unwrap(body, response.status)
        except urllib.error.HTTPError as error:  # node answered with a failure
            body = error.read().decode("utf-8", "replace")
            return self._unwrap(body, error.code)
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise ChainUnavailable(
                f"the TOR node at {self._base} is unreachable "
                f"({getattr(error, 'reason', error)}) — start it with "
                f"`torchain serve --bind {self._base.split('//')[-1]}`",
                details={"node": self._base},
            ) from error

    @staticmethod
    def _unwrap(body: str, status: int) -> dict:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "data": None,
                    "error": {"code": f"HTTP_{status}", "http_status": status,
                              "message": body[:400] or "empty response"}}
        if isinstance(parsed, dict) and "ok" in parsed:
            parsed.setdefault("data", None)
            parsed.setdefault("error", None)
            parsed.setdefault("meta", {})
            parsed["meta"].setdefault("http_status", status)
            return parsed
        return {"ok": True, "data": parsed, "error": None,
                "meta": {"http_status": status}}

    # -- health / reference ---------------------------------------------
    def health(self) -> dict:
        return self.call("GET", "/health")

    def info(self) -> dict:
        return self.call("GET", "/api/chain/info")

    def coin(self) -> dict:
        return self.call("GET", "/api/coin")

    def pricing(self) -> dict:
        return self.call("GET", "/api/pricing")

    def tokens(self) -> dict:
        return self.call("GET", "/api/tokens")

    def pools(self) -> dict:
        return self.call("GET", "/api/pools")

    def public_ledger(self, limit: int = 500, offset: int = 0) -> dict:
        """The permanent public ledger: balances, tokens, pools and totals."""
        return self.call("GET", "/api/public/ledger",
                         query={"limit": int(limit), "offset": int(offset)})

    # -- accounts --------------------------------------------------------
    def account(self, address: str) -> dict:
        return self.call("GET", f"/api/account/{urllib.parse.quote(str(address))}")

    def balance(self, address: str) -> dict:
        return self.account(address)

    def account_holdings(self, address: str) -> dict:
        """TOR + tokens + unique editions, joined with the chain token table.

        Answers the wallet's TOKEN HOLDINGS and NFT HOLDINGS panes for **any**
        address — including a wallet whose key lives in a product's own keystore
        (the webapp's ``WalletDesk``), which this node has never seen.
        """
        return self.call(
            "GET", f"/api/account/{urllib.parse.quote(str(address))}/holdings")

    def account_history(self, address: str, *, limit: int = 100,
                        offset: int = 0, token: str | None = None) -> dict:
        """That address's records, newest first, with direction + confirmations.

        ``token`` is the chain admin token: with it, records a previous release
        marked private are listed too (nothing on this release writes one).
        """
        return self.call(
            "GET", f"/api/account/{urllib.parse.quote(str(address))}/history",
            query={"limit": int(limit), "offset": int(offset)}, token=token)

    # -- NFTs: unique editions (supply 1, 0 decimals) ---------------------
    def nfts(self, limit: int = 200, offset: int = 0) -> dict:
        """Every unique edition on the chain, with the holder of its supply."""
        return self.call("GET", "/api/nft",
                         query={"limit": int(limit), "offset": int(offset)})

    def nft(self, symbol: str) -> dict:
        """One edition: current owner (derived from balances), creator, metadata."""
        return self.call("GET", f"/api/nft/{urllib.parse.quote(str(symbol))}")

    def account_nfts(self, address: str) -> dict:
        """The NFT pane of one wallet — an empty list, never a 404, for a new one."""
        return self.call(
            "GET", f"/api/account/{urllib.parse.quote(str(address))}/nfts")

    def nft_history(self, address: str | None = None, symbol: str | None = None,
                    limit: int = 200) -> dict:
        """Every NFT mint and transfer, newest first; narrowed by address/symbol."""
        query: dict[str, object] = {"limit": int(limit)}
        if address:
            query["address"] = address
        if symbol:
            query["symbol"] = symbol
        return self.call("GET", "/api/nft/history", query=query)

    def nft_transfer(self, signed: dict) -> dict:
        """Submit a signed ``nft_transfer`` (the node refuses every other kind)."""
        return self.call("POST", "/api/nft/transfer", payload=signed, timeout=30.0)

    def nft_receipt(self, txid: str, token: str | None = None) -> dict:
        """The transfer receipt: block, confirmations, owner now, destination holds."""
        return self.call("GET", f"/api/nft/tx/{urllib.parse.quote(str(txid))}",
                         token=token)

    # -- blocks / records ------------------------------------------------
    def blocks(self, start: int = 0, end: int | None = None) -> dict:
        return self.call("GET", "/api/chain/blocks",
                         query={"from": int(start), "to": end})

    def block(self, ref: int | str) -> dict:
        return self.call("GET", f"/api/chain/block/{urllib.parse.quote(str(ref))}")

    def tx(self, txid: str, token: str | None = None) -> dict:
        return self.call("GET", f"/api/chain/tx/{urllib.parse.quote(str(txid))}",
                         token=token)

    def events(self, limit: int = 50) -> dict:
        return self.call("GET", "/api/chain/events", query={"limit": int(limit)})

    def public_records(self, limit: int = 50) -> dict:
        return self.call("GET", "/api/public/records", query={"limit": int(limit)})

    def private_records(self, token: str | None = None, limit: int = 50) -> dict:
        """Records an older release sealed with its 30-day window (admin only)."""
        return self.call("POST", "/api/private/records", payload={},
                         query={"limit": int(limit)}, token=token)

    def submit_tx(self, signed: dict) -> dict:
        return self.call("POST", "/api/chain/tx", payload=signed, timeout=30.0)

    # -- replication -----------------------------------------------------
    def sync_export(self, token: str | None = None) -> dict:
        """The verified ledger snapshot (admin) — the replica pull source."""
        return self.call("GET", "/api/admin/sync/export", token=token,
                         timeout=120.0)

    def sync_import(self, snapshot: dict, token: str | None = None) -> dict:
        """Adopt a verified snapshot (admin); state.json + blocks.jsonl swap."""
        return self.call("POST", "/api/admin/sync/import", payload=snapshot,
                         token=token, timeout=300.0)

    # -- derived ---------------------------------------------------------
    def verify(self) -> dict:
        """Live integrity snapshot (height, supply, founder balance)."""
        info = self.info()
        if not info.get("ok"):
            return info
        data = info["data"]
        founder = str(data.get("founder", ""))
        founder_balance = 0
        if founder:
            account = self.account(founder)
            if account.get("ok"):
                founder_balance = int(account["data"].get("balance_micro") or 0)
        supply_micro = int(data.get("total_supply_micro")
                           or data.get("total_supply") or 0)
        return {"ok": True, "error": None, "meta": {}, "data": {
            "chain_id": data.get("chain_id"),
            "height": data.get("height"),
            "head_hash": data.get("head_hash"),
            "genesis_hash": data.get("genesis_hash"),
            "total_supply": supply_micro,
            "total_supply_micro": supply_micro,
            "supply_micro_tor": supply_micro,
            "founder": founder,
            "founder_balance": founder_balance,
            "founder_balance_micro": founder_balance,
            "fee_receiver": data.get("fee_receiver"),
            "checked_at": int(time.time()),
        }}

    def fee_receiver(self) -> str:
        info = self.info()
        if not info.get("ok"):
            raise ChainUnavailable(
                (info.get("error") or {}).get("message", "the TOR node is down"))
        data = info.get("data") or {}
        receiver = str(data.get("fee_receiver") or data.get("founder") or "")
        if not receiver:
            raise ValidationError("the node reports no fee receiver")
        return receiver

    def set_fee_receiver(self, address: str, token: str | None = None) -> dict:
        """Point platform/service fees at ``address`` (chain admin token)."""
        return self.call("POST", "/api/admin/fee-receiver",
                         payload={"address": str(address)}, token=token)

    def nonce_of(self, address: str) -> int:
        reply = self.account(address)
        return int(reply["data"].get("nonce", 0)) if reply.get("ok") else 0

    def is_reachable(self) -> bool:
        try:
            return bool(self.health().get("ok"))
        except ChainUnavailable:
            return False


# module-level default client (pages/services import this)
node = NodeClient()


def node_for(token: str | None) -> NodeClient:
    """Admin-scoped client for a request that carries ``token``."""
    return node.with_token(token)
