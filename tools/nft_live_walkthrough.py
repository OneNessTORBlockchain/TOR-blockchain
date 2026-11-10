"""Walk a native NFT transfer through a *live* API and print the evidence.

The tests prove the rules; this script is the reproduction anyone can run
against a running node — a laptop stack or a deployed one — and it exits
non-zero the moment a promise stops holding. Every line is an HTTP answer and
every signature is made by ``torchain_signer``.

    python tools/nft_live_walkthrough.py --to TOR1…onion
    python tools/nft_live_walkthrough.py --base http://127.0.0.1:8090 \
        --wallet data/wallets/founder.json --to TOR1…onion --wait 300

What it does, in order:

1. mints a supply-1 edition (``token_create``) if the symbol is not on chain yet;
2. reads the edition back and checks the holder is the minter;
3. refuses ``amount=2``, an unknown symbol, a transfer to the holder and a
   wrong ``kind`` — the four mistakes a wallet can make;
4. moves the edition anonymously to ``--to`` through
   ``POST /api/chain/nft/transfer``;
5. reads the ownership, both holdings panes, the catalogue, the history and the
   receipt back, and checks the *public* doors mask the sender while the
   receiver and the creator stay visible;
6. replays the same signed bytes (409 NONCE_MISMATCH) and re-signs as the old
   owner (403 UNAUTHORIZED).

``--wait SECONDS`` polls until the NFT doors exist, so the script can be left
running while a deploy lands. Exit codes: 0 every promise held, 1 one broke
(the report says which), 2 the doors never appeared.

One run per symbol: the holder must still hold the edition when the move
happens, so a second run against the same chain needs a new ``--symbol`` (an
edition that already moved answers 409 and the verdict names it).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.torchain_signer import sign_transaction  # noqa: E402

ANONYMOUS_REF = "oneness-privacy:1|mode=anonymous"
LABEL = "Tor-Anonymous-onion"


def call(session: requests.Session, base: str, path: str, body: dict | None = None,
         token: str | None = None, **params) -> dict:
    headers = {"x-admin-token": token} if token else {}
    if body is None:
        reply = session.get(base + path, headers=headers, params=params or None,
                            timeout=120)
    else:
        reply = session.post(base + path, json=body, headers=headers, timeout=120)
    try:
        return {"status": reply.status_code, **reply.json()}
    except ValueError:
        return {"status": reply.status_code, "raw": reply.text[:400]}


def wait_for_doors(session: requests.Session, base: str, seconds: float) -> str:
    """``""`` when the NFT doors answer, otherwise the reason they do not."""
    deadline = time.time() + seconds
    while True:
        try:
            reply = session.get(base + "/api/chain/nft", timeout=60)
            if reply.status_code == 200:
                return ""
            problem = f"/api/chain/nft answered {reply.status_code}"
        except requests.RequestException as error:
            problem = f"/api/chain/nft unreachable: {str(error)[:160]}"
        if time.time() >= deadline:
            return f"{problem} for {seconds:.0f}s"
        time.sleep(15)



def _data(step: dict | None) -> dict:
    value = (step or {}).get("data")
    return value if isinstance(value, dict) else {}


def _code(step: dict | None) -> str:
    return str(((step or {}).get("error") or {}).get("code") or "")


def verdict(report: dict) -> list[str]:
    """Every promise this walkthrough makes, checked against the answers."""
    steps = report["steps"]
    holder, to = report["holder"], report["recipient"]
    symbol = report["symbol"]
    broken: list[str] = []

    def want(label: str, ok: bool, detail: str = "") -> None:
        if not ok:
            broken.append(f"{label}{': ' + detail if detail else ''}")

    want("mint accepted", steps.get("mint", {}).get("status") == 200,
         str(steps.get("mint", {}).get("status")))
    minted = _data(steps.get("edition_after_mint"))
    want("the minter holds the edition",
         minted.get("owner") == holder and minted.get("supply") == 1,
         f"owner={minted.get('owner')} supply={minted.get('supply')}")

    want("amount=2 is 400", steps.get("amount_two", {}).get("status") == 400,
         str(steps.get("amount_two", {}).get("status")))
    want("an unknown symbol is 404 UNKNOWN_TOKEN",
         steps.get("unknown_symbol", {}).get("status") == 404
         and _code(steps.get("unknown_symbol")) == "UNKNOWN_TOKEN",
         str(steps.get("unknown_symbol", {}).get("status")))
    want("a transfer to the holder is 409",
         steps.get("to_the_holder", {}).get("status") == 409,
         str(steps.get("to_the_holder", {}).get("status")))
    want("a wrong kind is 400 at the door",
         steps.get("wrong_kind", {}).get("status") == 400,
         str(steps.get("wrong_kind", {}).get("status")))

    sale = _data(steps.get("transfer"))
    receipt = sale.get("receipt") if isinstance(sale.get("receipt"), dict) else {}
    transfer = receipt.get("transfer") if isinstance(receipt.get("transfer"), dict) else {}
    want("the move is accepted", steps.get("transfer", {}).get("status") == 200,
         str(steps.get("transfer", {}).get("status")))
    want("the receiver owns the edition now",
         (receipt.get("ownership") or {}).get("owner_now") == to,
         str((receipt.get("ownership") or {}).get("owner_now")))
    want("the public receipt masks the sender", transfer.get("from") == LABEL,
         str(transfer.get("from")))
    want("the public receipt keeps the receiver", transfer.get("to") == to,
         str(transfer.get("to")))

    public = _data(steps.get("edition_public"))
    last = public.get("last_transfer") if isinstance(public.get("last_transfer"), dict) else {}
    want("the public ownership door names the new holder", public.get("owner") == to,
         str(public.get("owner")))
    want("the creator is never masked", public.get("creator") == holder,
         str(public.get("creator")))
    want("the last move's sender is masked", last.get("from") == LABEL,
         str(last.get("from")))

    receiving = _data(steps.get("holdings_recipient")).get("nfts") or []
    keeping = _data(steps.get("holdings_holder")).get("nfts") or []
    want("the receiver's holdings pane carries the edition",
         any(row.get("symbol") == symbol for row in receiving if isinstance(row, dict)))
    want("the seller's holdings pane no longer carries it",
         not any(row.get("symbol") == symbol for row in keeping if isinstance(row, dict)))

    catalogue = _data(steps.get("catalogue")).get("nfts") or []
    want("the catalogue agrees who holds it",
         any(row.get("symbol") == symbol and row.get("owner") == to
             for row in catalogue if isinstance(row, dict)))

    history = _data(steps.get("history_public")).get("records") or []
    if history and isinstance(history[0], dict):
        want("history masks the sender", history[0].get("from") == LABEL,
             str(history[0].get("from")))
        want("history recomputes the counterparty", history[0].get("counterparty") == to,
             str(history[0].get("counterparty")))
    else:
        broken.append("history answered no records")

    if "receipt_public" in steps:
        shown = _data(steps.get("receipt_public"))
        shown_transfer = shown.get("transfer") if isinstance(shown.get("transfer"), dict) else {}
        want("the receipt door masks the same record",
             shown_transfer.get("from") == LABEL, str(shown_transfer.get("from")))
        want("the receipt door reports what it hid",
             shown.get("masked_fields") == ["from"], str(shown.get("masked_fields")))
        record = _data(steps.get("record_public"))
        want("the older /api/chain/tx door agrees", record.get("from") == LABEL,
             str(record.get("from")))
        if "receipt_admin" in steps:
            raw = _data(steps.get("receipt_admin"))
            raw_transfer = raw.get("transfer") if isinstance(raw.get("transfer"), dict) else {}
            want("the admin receipt reads the ledger as it is",
                 raw_transfer.get("from") == holder, str(raw_transfer.get("from")))

    want("a replay is 409 NONCE_MISMATCH",
         steps.get("replay", {}).get("status") == 409
         and _code(steps.get("replay")) == "NONCE_MISMATCH",
         str(steps.get("replay", {}).get("status")))
    want("the old owner can no longer move it",
         steps.get("old_owner_signs_again", {}).get("status") == 403,
         str(steps.get("old_owner_signs_again", {}).get("status")))
    return broken


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="https://www.torblockchain.fun",
                        help="the API to walk (it must answer /api/chain/nft)")
    parser.add_argument("--wallet", default=str(ROOT / "data" / "wallets" / "founder.json"),
                        help="the holder's keystore (signs the mint and the move)")
    parser.add_argument("--to", required=True, help="the receiving TOR1…onion")
    parser.add_argument("--symbol", default="CAPTN01", help="the edition's symbol")
    parser.add_argument("--name", default="Captain OneNess — first native NFT move")
    parser.add_argument("--metadata", default="https://1ness.online/nft/captn01.json")
    parser.add_argument("--admin-token", default="",
                        help="also read the unmasked doors (optional)")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="poll this many seconds for the NFT doors to exist")
    parser.add_argument("--out", default="", help="also write the JSON report here")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    wallet = json.loads(Path(args.wallet).read_text(encoding="utf-8"))
    holder = wallet["address"]
    session = requests.Session()
    report: dict = {"base": base, "holder": holder, "recipient": args.to,
                    "symbol": args.symbol.upper(), "steps": {}}
    steps = report["steps"]

    if args.wait:
        problem = wait_for_doors(session, base, args.wait)
        report["doors"] = problem or "live"
        if problem:
            print(json.dumps(report, indent=2))
            return 2

    def sign(kind: str, to: str, amount: int, token: str | None, note: str,
             ref: str, source: dict | None = None) -> dict:
        who = source or wallet
        seen = call(session, base, f"/api/account/{who['address']}")
        return sign_transaction(who, {
            "kind": kind, "from": who["address"], "to": to, "amount": amount,
            "token": token, "note": note, "external_ref": ref,
            "nonce": int((seen.get("data") or {}).get("nonce", 0)),
            "timestamp": int(time.time())})

    # 1. mint — a supply-1, 0-decimal token IS an NFT on this chain
    existing = call(session, base, f"/api/chain/nft/{report['symbol']}")
    if existing.get("ok"):
        steps["mint"] = {"status": existing.get("status"), "skipped": "already on chain"}
    else:
        steps["mint"] = call(session, base, "/api/chain/tx", sign(
            "token_create", holder, 1, report["symbol"], args.name, args.metadata))

    # 2. the edition answers with its holder
    steps["edition_after_mint"] = call(session, base, f"/api/chain/nft/{report['symbol']}")

    # 3. the four mistakes a wallet can make, each refused by name
    steps["amount_two"] = call(session, base, "/api/chain/nft/transfer", sign(
        "nft_transfer", args.to, 2, report["symbol"], "", ANONYMOUS_REF))
    steps["unknown_symbol"] = call(session, base, "/api/chain/nft/transfer", sign(
        "nft_transfer", args.to, 1, "NOSUCH", "", ANONYMOUS_REF))
    steps["to_the_holder"] = call(session, base, "/api/chain/nft/transfer", sign(
        "nft_transfer", holder, 1, report["symbol"], "", ANONYMOUS_REF))
    steps["wrong_kind"] = call(session, base, "/api/chain/nft/transfer", {
        **sign("transfer", args.to, 1, report["symbol"], "", ANONYMOUS_REF),
        "kind": "transfer"})

    # 4. the anonymous move, signed by the holder
    move = sign("nft_transfer", args.to, 1, report["symbol"],
                "native NFT walkthrough", ANONYMOUS_REF)
    sold = call(session, base, "/api/chain/nft/transfer", move)
    steps["transfer"] = sold
    txid = ((sold.get("data") or {}).get("receipt") or {}).get("txid")

    # 5. every read door, public first
    steps["edition_public"] = call(session, base, f"/api/chain/nft/{report['symbol']}")
    steps["holdings_recipient"] = call(session, base, f"/api/account/{args.to}/nfts")
    steps["holdings_holder"] = call(session, base, f"/api/account/{holder}/nfts")
    steps["catalogue"] = call(session, base, "/api/chain/nft")
    steps["history_public"] = call(session, base, "/api/chain/nft/history",
                                   symbol=report["symbol"])
    if txid:
        steps["receipt_public"] = call(session, base, f"/api/chain/nft/tx/{txid}")
        steps["record_public"] = call(session, base, f"/api/chain/tx/{txid}")
        if args.admin_token:
            steps["edition_admin"] = call(session, base,
                                          f"/api/chain/nft/{report['symbol']}",
                                          token=args.admin_token)
            steps["receipt_admin"] = call(session, base, f"/api/chain/nft/tx/{txid}",
                                          token=args.admin_token)

    # 6. the two refusals that prove ownership moved
    steps["replay"] = call(session, base, "/api/chain/nft/transfer", move)
    steps["old_owner_signs_again"] = call(session, base, "/api/chain/nft/transfer", sign(
        "nft_transfer", args.to, 1, report["symbol"], "", ANONYMOUS_REF))

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    broken = verdict(report)
    if broken:
        print("\nBROKEN PROMISES:", *broken, sep="\n  - ", file=sys.stderr)
        return 1
    print(f"\nevery promise held: {len(steps)} steps against {base}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

