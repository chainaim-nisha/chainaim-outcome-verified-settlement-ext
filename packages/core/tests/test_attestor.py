"""Group D tests -- the attestor tier guard (:mod:`chainaim_settlement_core.attestor`).

Covers the build-plan targets: hash-only skips the signature step; signed REQUIRES a
delivery attestation, verifies it against the trusted-pubkey allow-list, and rejects an
untrusted key, a swapped payload, or a bad signature. Signature VERIFICATION, not a
key-identity match (contract S9). Group D is independent of Group C -- no engine, no async.
"""

import hashlib

from chainaim_settlement_core.attestor import (
    AttestorDecision,
    TrustedKeys,
    attestation_message,
    clear_for_release,
    sign_attestation,
)
from chainaim_settlement_core.contract import (
    Attestation,
    Attestor,
    UnitIn,
    canonical_bytes,
)
from chainaim_settlement_core.signing import SettlementSigner, load_public_key

REF = hashlib.sha256(b"deal-attestor").hexdigest()


def _unit(seq: int = 0, body: str = "invoice") -> UnitIn:
    chunk = canonical_bytes({"seq": seq, "task_id": REF, "body": body})
    return UnitIn(
        seq=seq,
        payload_hex=chunk.hex(),
        declared_checksum=hashlib.sha256(chunk).hexdigest(),
    )


# --------------------------------------------------------------------------- #
# TrustedKeys allow-list.
# --------------------------------------------------------------------------- #


def test_trusted_keys_membership_is_case_insensitive_on_hex():
    pk = SettlementSigner.generate().public_key_hex
    ring = TrustedKeys([pk.upper()])
    assert pk in ring
    assert pk.upper() in ring
    assert len(ring) == 1


def test_trusted_keys_empty_contains_nothing():
    assert SettlementSigner.generate().public_key_hex not in TrustedKeys()


def test_trusted_keys_with_key_is_immutable_update():
    base = TrustedKeys()
    pk = SettlementSigner.generate().public_key_hex
    extended = base.with_key(pk)
    assert pk in extended
    assert pk not in base  # original unchanged


# --------------------------------------------------------------------------- #
# hash-only: the guard is a no-op.
# --------------------------------------------------------------------------- #


def test_hash_only_clears_without_attestation():
    d = clear_for_release(
        attestor=Attestor.HASH_ONLY, ref=REF, unit=_unit(), attestation=None
    )
    assert d == AttestorDecision(True, "hash-only")


def test_hash_only_ignores_a_present_attestation():
    builder = SettlementSigner.generate()
    att = sign_attestation(REF, _unit(), signer=builder)
    d = clear_for_release(
        attestor=Attestor.HASH_ONLY, ref=REF, unit=_unit(), attestation=att
    )
    assert d.cleared is True
    assert d.reason == "hash-only"


# --------------------------------------------------------------------------- #
# signed: happy path.
# --------------------------------------------------------------------------- #


def test_signed_clears_with_trusted_and_valid_attestation():
    builder = SettlementSigner.generate()
    unit = _unit()
    att = sign_attestation(REF, unit, signer=builder)
    ring = TrustedKeys([builder.public_key_hex])
    d = clear_for_release(
        attestor=Attestor.SIGNED, ref=REF, unit=unit, attestation=att, trusted_keys=ring
    )
    assert d == AttestorDecision(True, "attested")


def test_sign_attestation_produces_message_signed_over_ref_and_unit():
    builder = SettlementSigner.generate()
    unit = _unit(seq=3)
    att = sign_attestation(REF, unit, signer=builder)
    assert att.signer_pubkey == builder.public_key_hex
    # The signed bytes are exactly the frozen attestation message.
    load_public_key(att.signer_pubkey).verify(
        bytes.fromhex(att.sig), attestation_message(REF, unit)
    )  # raises if it does not match; passing means bound to (ref, unit)


# --------------------------------------------------------------------------- #
# signed: rejections (fail closed).
# --------------------------------------------------------------------------- #


def test_signed_requires_attestation():
    d = clear_for_release(
        attestor=Attestor.SIGNED,
        ref=REF,
        unit=_unit(),
        attestation=None,
        trusted_keys=TrustedKeys([SettlementSigner.generate().public_key_hex]),
    )
    assert d == AttestorDecision(False, "attestation-required")


def test_signed_with_no_allow_list_fails_closed():
    builder = SettlementSigner.generate()
    unit = _unit()
    att = sign_attestation(REF, unit, signer=builder)
    # trusted_keys omitted -> treated as empty -> a valid sig from an unknown key is refused.
    d = clear_for_release(attestor=Attestor.SIGNED, ref=REF, unit=unit, attestation=att)
    assert d == AttestorDecision(False, "untrusted-pubkey")


def test_signed_rejects_untrusted_pubkey():
    builder, stranger = SettlementSigner.generate(), SettlementSigner.generate()
    unit = _unit()
    att = sign_attestation(REF, unit, signer=builder)  # validly signed...
    ring = TrustedKeys([stranger.public_key_hex])  # ...but builder not on the list
    d = clear_for_release(
        attestor=Attestor.SIGNED, ref=REF, unit=unit, attestation=att, trusted_keys=ring
    )
    assert d == AttestorDecision(False, "untrusted-pubkey")


def test_signed_rejects_swapped_payload():
    builder = SettlementSigner.generate()
    unit_a = _unit(seq=0, body="the-committed-artifact")
    att = sign_attestation(REF, unit_a, signer=builder)  # attestation is for unit_a
    ring = TrustedKeys([builder.public_key_hex])
    unit_b = _unit(seq=0, body="a-different-artifact")  # same seq, different bytes
    d = clear_for_release(
        attestor=Attestor.SIGNED,
        ref=REF,
        unit=unit_b,
        attestation=att,
        trusted_keys=ring,
    )
    assert d == AttestorDecision(False, "bad-attestation-sig")


def test_signed_rejects_wrong_ref():
    builder = SettlementSigner.generate()
    unit = _unit()
    att = sign_attestation(REF, unit, signer=builder)
    ring = TrustedKeys([builder.public_key_hex])
    other_ref = hashlib.sha256(b"another-deal").hexdigest()
    d = clear_for_release(
        attestor=Attestor.SIGNED,
        ref=other_ref,
        unit=unit,
        attestation=att,
        trusted_keys=ring,
    )
    assert d == AttestorDecision(False, "bad-attestation-sig")


def test_signed_rejects_bad_signature_hex():
    builder = SettlementSigner.generate()
    unit = _unit()
    ring = TrustedKeys([builder.public_key_hex])
    non_hex = Attestation(signer_pubkey=builder.public_key_hex, sig="zz-not-hex")
    wrong = Attestation(
        signer_pubkey=builder.public_key_hex, sig="deadbeef"
    )  # hex, wrong len
    d1 = clear_for_release(
        attestor=Attestor.SIGNED,
        ref=REF,
        unit=unit,
        attestation=non_hex,
        trusted_keys=ring,
    )
    d2 = clear_for_release(
        attestor=Attestor.SIGNED,
        ref=REF,
        unit=unit,
        attestation=wrong,
        trusted_keys=ring,
    )
    assert d1 == AttestorDecision(False, "bad-attestation-sig")
    assert d2 == AttestorDecision(False, "bad-attestation-sig")


def test_signed_rejects_malformed_pubkey_on_allow_list():
    unit = _unit()
    bad_pk = "abcd"  # on the list but not a valid 32-byte Ed25519 key
    att = Attestation(signer_pubkey=bad_pk, sig="00" * 64)
    ring = TrustedKeys([bad_pk])
    d = clear_for_release(
        attestor=Attestor.SIGNED, ref=REF, unit=unit, attestation=att, trusted_keys=ring
    )
    assert d == AttestorDecision(False, "bad-pubkey")


# --------------------------------------------------------------------------- #
# NON-PAYEE enforcement (contract S9), only when the payee key is known.
# --------------------------------------------------------------------------- #


def test_signed_rejects_payee_signed_attestation():
    payee = SettlementSigner.generate()
    unit = _unit()
    att = sign_attestation(REF, unit, signer=payee)  # payee co-signs its own delivery
    ring = TrustedKeys([payee.public_key_hex])  # even if trusted...
    d = clear_for_release(
        attestor=Attestor.SIGNED,
        ref=REF,
        unit=unit,
        attestation=att,
        trusted_keys=ring,
        payee_pubkey=payee.public_key_hex,  # ...the non-payee rule rejects it
    )
    assert d == AttestorDecision(False, "payee-signed")


def test_signed_clears_non_payee_signer_when_payee_known():
    builder, payee = SettlementSigner.generate(), SettlementSigner.generate()
    unit = _unit()
    att = sign_attestation(REF, unit, signer=builder)
    ring = TrustedKeys([builder.public_key_hex])
    d = clear_for_release(
        attestor=Attestor.SIGNED,
        ref=REF,
        unit=unit,
        attestation=att,
        trusted_keys=ring,
        payee_pubkey=payee.public_key_hex,
    )
    assert d == AttestorDecision(True, "attested")


# --------------------------------------------------------------------------- #
# Frozen attestation message binds ref + unit.
# --------------------------------------------------------------------------- #


def test_attestation_message_changes_with_ref_and_unit():
    u0, u1 = _unit(seq=0), _unit(seq=1)
    other_ref = hashlib.sha256(b"x").hexdigest()
    base = attestation_message(REF, u0)
    assert attestation_message(REF, u1) != base  # different unit
    assert attestation_message(other_ref, u0) != base  # different ref


def test_attestation_message_is_canonical_over_ref_and_unit():
    unit = _unit()
    assert attestation_message(REF, unit) == canonical_bytes(
        {"ref": REF, "unit": unit.model_dump(mode="json")}
    )
