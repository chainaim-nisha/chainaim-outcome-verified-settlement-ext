"""Group D tests -- Ed25519 verdict signing (:mod:`chainaim_settlement_core.signing`).

Covers the build-plan targets: keygen, sign/verify round-trip, and tamper detection (a
mutated receipt fact or a mutated trace line invalidates the signature). Group D is
independent of Group C, so these tests build a :class:`Receipt` directly from the contract
rather than driving the async engine -- no engine import, no asyncio.

Tests import from the submodule path (chainaim_settlement_core.signing), matching the
frozen-green convention in the other Groups.
"""

import hashlib

from chainaim_settlement_core.contract import PerUnit, Receipt, canonical_bytes
from chainaim_settlement_core.signing import (
    SettlementSigner,
    load_public_key,
    public_key_hex,
    sign_verdict,
    verdict_message,
    verify_verdict,
)

REF = hashlib.sha256(b"deal-signing").hexdigest()


def _receipt(*, settled: int = 200, remainder: int = 300) -> Receipt:
    return Receipt(
        ref=REF,
        per_unit=[
            PerUnit.model_validate(
                {"seq": 0, "verdict": "pass", "reason": "artifact-match"}
            ),
            PerUnit.model_validate(
                {"seq": 1, "verdict": "pass", "reason": "artifact-match"}
            ),
        ],
        settled_total=settled,
        remainder_unspent=remainder,
    )


TRACE = [
    f"stream-open:{REF}:buyer:seller:100:500:0",
    f"gate:{REF}:0:pass",
    f"tick:{REF}:0:100:1",
    f"gate:{REF}:1:pass",
    f"tick:{REF}:1:100:2",
]


# --------------------------------------------------------------------------- #
# Keygen + encoding.
# --------------------------------------------------------------------------- #


def test_generate_produces_distinct_keys():
    a, b = SettlementSigner.generate(), SettlementSigner.generate()
    assert a.public_key_hex != b.public_key_hex


def test_public_key_hex_is_64_char_lowercase_hex():
    pk = SettlementSigner.generate().public_key_hex
    assert len(pk) == 64
    assert pk == pk.lower()
    bytes.fromhex(pk)  # parses as hex


def test_public_key_hex_helper_matches_signer_property():
    signer = SettlementSigner.generate()
    assert public_key_hex(signer.public_key) == signer.public_key_hex


def test_load_public_key_round_trips_hex():
    signer = SettlementSigner.generate()
    loaded = load_public_key(signer.public_key_hex)
    assert public_key_hex(loaded) == signer.public_key_hex


def test_seed_round_trip_reproduces_key_and_signature():
    signer = SettlementSigner.generate()
    clone = SettlementSigner.from_seed_hex(signer.seed_hex())
    assert clone.public_key_hex == signer.public_key_hex
    # Ed25519 is deterministic: same key + same message -> identical signature.
    r, t = _receipt(), TRACE
    assert clone.sign(r, t) == signer.sign(r, t)


# --------------------------------------------------------------------------- #
# Frozen signable message.
# --------------------------------------------------------------------------- #


def test_verdict_message_is_canonical_over_receipt_and_trace():
    r = _receipt()
    assert verdict_message(r, TRACE) == canonical_bytes(
        {"receipt": r.model_dump(mode="json"), "trace": list(TRACE)}
    )


def test_verdict_message_is_stable_across_calls():
    r = _receipt()
    assert verdict_message(r, TRACE) == verdict_message(r, TRACE)


# --------------------------------------------------------------------------- #
# Sign / verify round-trip.
# --------------------------------------------------------------------------- #


def test_sign_then_verify_round_trip():
    signer = SettlementSigner.generate()
    r = _receipt()
    sig = signer.sign(r, TRACE)
    assert verify_verdict(r, TRACE, sig, public_key=signer.public_key) is True


def test_signature_is_128_char_lowercase_hex():
    sig = SettlementSigner.generate().sign(_receipt(), TRACE)
    assert len(sig) == 128
    assert sig == sig.lower()
    bytes.fromhex(sig)


def test_module_sign_verdict_matches_signer_method():
    signer = SettlementSigner.generate()
    r = _receipt()
    assert sign_verdict(r, TRACE, signer=signer) == signer.sign(r, TRACE)


def test_verify_accepts_public_key_as_hex_or_object():
    signer = SettlementSigner.generate()
    r = _receipt()
    sig = signer.sign(r, TRACE)
    assert verify_verdict(r, TRACE, sig, public_key=signer.public_key) is True
    assert verify_verdict(r, TRACE, sig, public_key=signer.public_key_hex) is True


def test_signing_is_deterministic():
    signer = SettlementSigner.generate()
    r = _receipt()
    assert signer.sign(r, TRACE) == signer.sign(r, TRACE)


# --------------------------------------------------------------------------- #
# Tamper detection -- the whole point of signing.
# --------------------------------------------------------------------------- #


def test_tamper_receipt_settled_total_fails():
    signer = SettlementSigner.generate()
    sig = signer.sign(_receipt(settled=200, remainder=300), TRACE)
    # Same ref/verdicts, but the money moved -> different signable bytes -> invalid.
    tampered = _receipt(settled=300, remainder=200)
    assert verify_verdict(tampered, TRACE, sig, public_key=signer.public_key) is False


def test_tamper_receipt_verdict_fails():
    signer = SettlementSigner.generate()
    r = _receipt()
    sig = signer.sign(r, TRACE)
    flipped = Receipt(
        ref=REF,
        per_unit=[
            PerUnit.model_validate(
                {"seq": 0, "verdict": "pass", "reason": "artifact-match"}
            ),
            PerUnit.model_validate(
                {"seq": 1, "verdict": "fail", "reason": "artifact-mismatch"}
            ),  # flipped
        ],
        settled_total=r.settled_total,
        remainder_unspent=r.remainder_unspent,
    )
    assert verify_verdict(flipped, TRACE, sig, public_key=signer.public_key) is False


def test_tamper_trace_line_fails():
    signer = SettlementSigner.generate()
    r = _receipt()
    sig = signer.sign(r, TRACE)
    mutated = list(TRACE)
    mutated[2] = f"tick:{REF}:0:500:1"  # a released amount was rewritten
    assert verify_verdict(r, mutated, sig, public_key=signer.public_key) is False


def test_wrong_public_key_fails():
    signer, other = SettlementSigner.generate(), SettlementSigner.generate()
    r = _receipt()
    sig = signer.sign(r, TRACE)
    assert verify_verdict(r, TRACE, sig, public_key=other.public_key) is False


def test_garbage_signature_returns_false_not_raises():
    signer = SettlementSigner.generate()
    r = _receipt()
    assert verify_verdict(r, TRACE, "not-hex-!!", public_key=signer.public_key) is False
    assert (
        verify_verdict(r, TRACE, "abcd", public_key=signer.public_key) is False
    )  # too short
    assert verify_verdict(r, TRACE, "", public_key=signer.public_key) is False


def test_malformed_public_key_returns_false_not_raises():
    r = _receipt()
    sig = SettlementSigner.generate().sign(r, TRACE)
    assert verify_verdict(r, TRACE, sig, public_key="00ff") is False  # not 32 bytes
