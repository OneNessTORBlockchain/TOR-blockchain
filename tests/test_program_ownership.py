"""Only the program owner can view everything and manage the program."""
from __future__ import annotations

import pytest

from core.errors import Forbidden
from oneness_tor.services import contracts, wallet_access


def client():
    from oneness_tor.app import create_app

    return create_app().test_client()


def _program(make_wallet, name: str = "u_owner") -> tuple[dict, dict]:
    owner = make_wallet(name)
    program = contracts.deploy_program(
        build_id="build_0001", name="owner program", wallet=name,
        source="fn main() { println!(\"hi\"); }",
        binary_path="data/workbench/build_0001/target/release/app.exe",
        binary_sha3="a" * 64)
    return owner, program


def test_summary_hides_the_innards_and_owner_sees_all(sandbox, make_wallet):
    owner, program = _program(make_wallet)

    public = contracts.get_contract(program["id"])
    assert public["access"] == "summary"
    for hidden in ("source", "state", "compile", "binary_path", "charge", "txid"):
        assert hidden not in public
    assert public["owner_address"] == owner["address"]
    assert public["binary_sha3"] == "a" * 64

    mine = contracts.get_contract(program["id"], viewer_address=owner["address"])
    assert mine["access"] == "owner"
    assert mine["source"].startswith("fn main")
    assert mine["binary_path"].endswith(".exe")
    assert mine["compile"]["ok"] is True


def test_only_the_owner_can_manage_a_program(sandbox, make_wallet):
    owner, program = _program(make_wallet)
    stranger = make_wallet("u_stranger")

    with pytest.raises(Forbidden):
        contracts.call(program["id"], "status",
                       actor_address=stranger["address"])
    managed = contracts.call(program["id"], "status",
                             actor_address=owner["address"])
    assert managed["state"]["last_call"]["by"] == owner["address"]
    assert managed["state"]["calls"] == 1


def test_owner_and_stranger_sessions_through_the_api(sandbox, make_wallet):
    owner, program = _program(make_wallet)
    stranger = make_wallet("u_stranger")
    owner_session = wallet_access.open_session("u_owner", owner["address"])
    stranger_session = wallet_access.open_session("u_stranger", stranger["address"])
    http = client()

    public = http.get(f"/api/contracts/{program['id']}")
    assert public.status_code == 200
    assert public.get_json()["data"]["contract"]["access"] == "summary"

    seen = http.get(f"/api/contracts/{program['id']}",
                    headers={wallet_access.HEADER: owner_session["session"]})
    assert seen.status_code == 200
    assert seen.get_json()["data"]["contract"]["access"] == "owner"

    denied = http.post(f"/api/contracts/{program['id']}/call",
                       json={"function": "status"},
                       headers={wallet_access.HEADER: stranger_session["session"]})
    assert denied.status_code == 403

    allowed = http.post(f"/api/contracts/{program['id']}/call",
                        json={"function": "status"},
                        headers={wallet_access.HEADER: owner_session["session"]})
    assert allowed.status_code == 200, allowed.get_data(as_text=True)
    assert allowed.get_json()["data"]["state"]["last_call"]["by"] == owner["address"]

    mine = http.get("/api/contracts/mine",
                    headers={wallet_access.HEADER: owner_session["session"]})
    assert mine.status_code == 200
    assert mine.get_json()["data"]["count"] == 1

    assert http.get("/api/contracts/mine").status_code == 400


def test_owner_address_is_the_deploying_wallet(sandbox, make_wallet):
    owner, program = _program(make_wallet)
    listed = contracts.list_contracts(viewer_address=owner["address"])
    assert listed["contracts"][0]["access"] == "owner"
    other = contracts.list_contracts(viewer_address="TOR1" + "a" * 32 + "onion")
    assert other["contracts"][0]["access"] == "summary"
