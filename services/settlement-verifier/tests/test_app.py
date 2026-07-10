"""Group E tests -- the settlement-verifier FastAPI shell (``app.py``).

Covers the build-plan targets via the TestClient: the house trio; the self-contained
/demo/settlement (match vs mismatch); open -> advance -> close happy path; a wrong unit
refused; the signed-tier attestation guard (missing / untrusted / valid); and the offline
/verify verdict-signature re-check. The engine is async; the TestClient drives the async
endpoints synchronously, so no pytest-asyncio is needed.
"""

import hashlib

from chainaim_settlement_core.attestor import sign_attestation
from chainaim_settlement_core.contract import PerUnit, Receipt, UnitIn, canonical_bytes

REF = hashlib.sha256(b"app-deal").hexdigest()


def _artifact(seq: int, body: str = "invoice") -> tuple[str, str]:
    chunk = canonical_bytes({"seq": seq, "task_id": REF, "body": body})
    return chunk.hex(), hashlib.sha256(chunk).hexdigest()


def _open_body(attestor: str = "hash-only") -> dict:
    return {
        "ref": REF,
        "rate": 100,
        "max_total": 500,
        "attestor": attestor,
        "committed_reference": {
            "criterion": "artifact_match",
            "task_id": REF,
            "expected": {"0": _artifact(0)[1], "1": _artifact(1)[1]},
        },
    }


def _unit(seq: int, body: str = "invoice") -> UnitIn:
    ph, sha = _artifact(seq, body)
    return UnitIn(seq=seq, payload_hex=ph, declared_checksum=sha)


def _advance_body(unit: UnitIn, attestation: dict | None = None) -> dict:
    body: dict = {"ref": REF, "unit": unit.model_dump(mode="json")}
    if attestation is not None:
        body["attestation"] = attestation
    return body


# --------------------------------------------------------------------------- #
# House trio.
# --------------------------------------------------------------------------- #


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "settlement-verifier"}


def test_skill_md_served_verbatim(client):
    r = client.get("/skill.md")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "display" in body
    assert "verbatim" in body


def test_pubkey_matches_configured_signer(client, verifier_signer):
    r = client.get("/pubkey")
    assert r.status_code == 200
    data = r.json()
    assert data["algo"] == "ed25519"
    assert data["pubkey"] == verifier_signer.public_key_hex
    assert len(data["pubkey"]) == 64


# --------------------------------------------------------------------------- #
# Self-contained demo.
# --------------------------------------------------------------------------- #


def test_demo_good_settles_every_unit(client):
    r = client.get("/demo/settlement", params={"case": "good"})
    assert r.status_code == 200
    out = r.json()
    assert [u["verdict"] for u in out["per_unit"]] == ["pass", "pass"]
    assert out["settled_total"] == 200
    assert out["remainder_unspent"] == 300
    assert out["verdict_signature"] is None  # hash-only demo
    assert out["display"]


def test_demo_bad_settles_one_then_fails_and_keeps_remainder(client):
    r = client.get("/demo/settlement", params={"case": "bad"})
    assert r.status_code == 200
    out = r.json()
    assert [u["verdict"] for u in out["per_unit"]] == ["pass", "fail"]
    assert out["per_unit"][1]["reason"] == "artifact-mismatch"
    assert out["settled_total"] == 100
    assert out["remainder_unspent"] == 400
    assert any(
        line.startswith("stream-close:") and line.endswith(":artifact-mismatch")
        for line in out["trace"]
    )


def test_demo_defaults_to_good(client):
    assert client.get("/demo/settlement").json()["settled_total"] == 200


def test_demo_rejects_unknown_case(client):
    assert (
        client.get("/demo/settlement", params={"case": "sideways"}).status_code == 422
    )


# --------------------------------------------------------------------------- #
# open -> advance -> close (hash-only).
# --------------------------------------------------------------------------- #


def test_open_advance_close_happy_path(client):
    assert client.post("/settle/open", json=_open_body()).status_code == 200

    a0 = client.post("/settle/advance", json=_advance_body(_unit(0))).json()
    assert a0["per_unit"][0]["verdict"] == "pass"
    assert a0["settled_total"] == 100

    a1 = client.post("/settle/advance", json=_advance_body(_unit(1))).json()
    assert a1["settled_total"] == 200

    close = client.post("/settle/close", json={"ref": REF}).json()
    assert [u["verdict"] for u in close["per_unit"]] == ["pass", "pass"]
    assert close["remainder_unspent"] == 300
    assert close["verdict_signature"] is None  # hash-only


def test_advance_wrong_unit_refused_and_remainder_unspent(client):
    client.post("/settle/open", json=_open_body())
    client.post("/settle/advance", json=_advance_body(_unit(0)))
    # seq 1 delivered with a DIFFERENT artifact, honestly checksummed -> L3 mismatch.
    ph, sha = _artifact(99, "wrong-bytes")
    bad = _advance_body(UnitIn(seq=1, payload_hex=ph, declared_checksum=sha))
    out = client.post("/settle/advance", json=bad).json()
    assert out["per_unit"][1]["verdict"] == "fail"
    assert out["per_unit"][1]["reason"] == "artifact-mismatch"
    assert out["settled_total"] == 100
    assert out["remainder_unspent"] == 400


def test_advance_unknown_ref_is_404(client):
    r = client.post("/settle/advance", json=_advance_body(_unit(0)))
    assert r.status_code == 404


def test_open_duplicate_ref_is_409(client):
    assert client.post("/settle/open", json=_open_body()).status_code == 200
    assert client.post("/settle/open", json=_open_body()).status_code == 409


def test_close_unknown_ref_is_404(client):
    assert client.post("/settle/close", json={"ref": REF}).status_code == 404


# --------------------------------------------------------------------------- #
# Signed tier -- the attestation guard + verdict signature.
# --------------------------------------------------------------------------- #


def test_signed_advance_with_trusted_attestation_settles_and_signs(client, builder):
    client.post("/settle/open", json=_open_body(attestor="signed"))
    unit = _unit(0)
    att = sign_attestation(REF, unit, signer=builder).model_dump(mode="json")
    out = client.post("/settle/advance", json=_advance_body(unit, att)).json()
    assert out["per_unit"][0]["verdict"] == "pass"
    assert out["settled_total"] == 100
    assert out["verdict_signature"] is not None
    assert len(out["verdict_signature"]) == 128


def test_signed_advance_missing_attestation_is_403(client):
    client.post("/settle/open", json=_open_body(attestor="signed"))
    r = client.post("/settle/advance", json=_advance_body(_unit(0)))
    assert r.status_code == 403
    assert r.json()["detail"] == "attestation-required"


def test_signed_advance_untrusted_attestation_is_403(client):
    from chainaim_settlement_core.signing import SettlementSigner

    client.post("/settle/open", json=_open_body(attestor="signed"))
    unit = _unit(0)
    stranger = SettlementSigner.generate()  # not on the allow-list
    att = sign_attestation(REF, unit, signer=stranger).model_dump(mode="json")
    r = client.post("/settle/advance", json=_advance_body(unit, att))
    assert r.status_code == 403
    assert r.json()["detail"] == "untrusted-pubkey"


# --------------------------------------------------------------------------- #
# /verify -- offline verdict-signature re-check.
# --------------------------------------------------------------------------- #


def _signed_close(client, builder) -> dict:
    """Run a signed-tier settlement to completion and return the close SettleOut json."""
    client.post("/settle/open", json=_open_body(attestor="signed"))
    unit = _unit(0)
    att = sign_attestation(REF, unit, signer=builder).model_dump(mode="json")
    client.post("/settle/advance", json=_advance_body(unit, att))
    return client.post("/settle/close", json={"ref": REF}).json()


def test_verify_accepts_a_genuine_verdict_signature(client, builder):
    out = _signed_close(client, builder)
    receipt = Receipt(
        ref=REF,
        per_unit=[PerUnit(**u) for u in out["per_unit"]],
        settled_total=out["settled_total"],
        remainder_unspent=out["remainder_unspent"],
    ).model_dump(mode="json")
    r = client.post(
        "/verify",
        json={
            "receipt": receipt,
            "trace": out["trace"],
            "verdict_signature": out["verdict_signature"],
        },
    )
    assert r.status_code == 200
    assert r.json() == {"valid": True, "reason": "signature-valid"}


def test_verify_detects_a_tampered_receipt(client, builder):
    out = _signed_close(client, builder)
    receipt = Receipt(
        ref=REF,
        per_unit=[PerUnit(**u) for u in out["per_unit"]],
        settled_total=out["settled_total"] + 100,  # tamper: claim more settled
        remainder_unspent=out["remainder_unspent"] - 100,
    ).model_dump(mode="json")
    r = client.post(
        "/verify",
        json={
            "receipt": receipt,
            "trace": out["trace"],
            "verdict_signature": out["verdict_signature"],
        },
    )
    assert r.json() == {"valid": False, "reason": "signature-invalid"}


def test_verify_reports_no_signature_for_hash_only(client):
    client.post("/settle/open", json=_open_body())  # hash-only
    client.post("/settle/advance", json=_advance_body(_unit(0)))
    out = client.post("/settle/close", json={"ref": REF}).json()
    assert out["verdict_signature"] is None
    receipt = Receipt(
        ref=REF,
        per_unit=[PerUnit(**u) for u in out["per_unit"]],
        settled_total=out["settled_total"],
        remainder_unspent=out["remainder_unspent"],
    ).model_dump(mode="json")
    r = client.post(
        "/verify",
        json={"receipt": receipt, "trace": out["trace"], "verdict_signature": None},
    )
    assert r.json() == {"valid": False, "reason": "no-signature"}
