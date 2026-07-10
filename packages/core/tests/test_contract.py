"""Group B tests -- the FROZEN data contract (v1.0).

Covers three things the build plan calls out:
  1. canonical-bytes determinism / stability / fidelity to the frozen S6 rule,
  2. committed_hash ties a commitment to a delivered chunk (the L3 good-path identity),
  3. pydantic IN/OUT validation, including that ``expected`` is a {seq: sha256} MAP
     (incl. the 1-entry degenerate case) and the frozen cross-field invariants.

Tests import from the submodule path (chainaim_settlement_core.contract) rather than a
re-export, so Group B does not have to touch Group A's already-green __init__.py.
"""

import hashlib
import json

import pytest
from pydantic import ValidationError

from chainaim_settlement_core.contract import (
    AdvanceIn,
    Attestation,
    Attestor,
    CommittedReference,
    Criterion,
    PerUnit,
    Receipt,
    SettleOpenIn,
    SettleOut,
    UnitIn,
    Verdict,
    canonical_bytes,
    committed_hash,
)

# Realistic fixtures: ref == a deal's trace_sha256; committed hashes are real sha256s.
REF = hashlib.sha256(b"deal-ph2-combo-1").hexdigest()
SHA0 = hashlib.sha256(b"artifact-0").hexdigest()
SHA8 = hashlib.sha256(b"artifact-8").hexdigest()
SHA9 = hashlib.sha256(b"artifact-9").hexdigest()


# --------------------------------------------------------------------------- #
# canonical_bytes -- the S6 rule.
# --------------------------------------------------------------------------- #


def test_canonical_bytes_sorts_keys_and_is_compact():
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_canonical_bytes_sorts_nested_keys():
    assert canonical_bytes({"z": {"b": 1, "a": 2}}) == b'{"z":{"a":2,"b":1}}'


def test_canonical_bytes_has_no_spaces():
    out = canonical_bytes({"a": 1, "b": [1, 2, 3], "c": {"d": 4}})
    assert b" " not in out


def test_canonical_bytes_is_key_order_independent():
    assert canonical_bytes({"a": 1, "b": 2}) == canonical_bytes({"b": 2, "a": 1})


def test_canonical_bytes_preserves_list_order():
    # Sorting applies to dict keys, never to list elements (unit order is meaningful).
    assert canonical_bytes({"x": [3, 1, 2]}) == b'{"x":[3,1,2]}'


def test_canonical_bytes_is_stable_across_calls():
    obj = {"seq": 0, "task_id": REF, "kind": "invoice", "amount": 500}
    assert canonical_bytes(obj) == canonical_bytes(obj)


def test_canonical_bytes_matches_the_frozen_rule_expression():
    # Fidelity: our function must equal the exact frozen S6 call (default ensure_ascii).
    obj = {"k": "caf\u00e9", "n": 2, "a": [3, 1, 2], "nested": {"y": 1, "x": 2}}
    assert canonical_bytes(obj) == json.dumps(
        obj, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


# --------------------------------------------------------------------------- #
# committed_hash.
# --------------------------------------------------------------------------- #


def test_committed_hash_is_sha256_of_canonical_bytes():
    obj = {"seq": 0, "task_id": REF, "amount": 500}
    assert committed_hash(obj) == hashlib.sha256(canonical_bytes(obj)).hexdigest()


def test_committed_hash_is_64_char_lowercase_hex():
    h = committed_hash({"a": 1})
    assert len(h) == 64
    assert h == h.lower()
    int(h, 16)  # parses as hex


def test_committed_hash_is_key_order_independent():
    assert committed_hash({"a": 1, "b": 2}) == committed_hash({"b": 2, "a": 1})


# --------------------------------------------------------------------------- #
# CommittedReference -- expected is a {seq: sha256} MAP (decision 1).
# --------------------------------------------------------------------------- #


def test_committed_reference_single_entry_degenerate_map():
    cr = CommittedReference(
        criterion=Criterion.ARTIFACT_MATCH, task_id=REF, expected={"0": SHA0}
    )
    assert cr.expected == {"0": SHA0}
    assert cr.criterion is Criterion.ARTIFACT_MATCH


def test_committed_reference_multi_entry_map():
    cr = CommittedReference.model_validate(
        {
            "criterion": "artifact_match",
            "task_id": REF,
            "expected": {"8": SHA8, "9": SHA9},
        }
    )
    assert cr.expected == {"8": SHA8, "9": SHA9}


def test_committed_reference_artifact_match_empty_map_rejected():
    with pytest.raises(ValidationError):
        CommittedReference.model_validate(
            {"criterion": "artifact_match", "task_id": REF, "expected": {}}
        )


def test_committed_reference_checksum_may_have_empty_map():
    cr = CommittedReference.model_validate(
        {"criterion": "checksum", "task_id": REF, "expected": {}}
    )
    assert cr.expected == {}


def test_committed_reference_ack_received_may_have_empty_map():
    cr = CommittedReference.model_validate(
        {"criterion": "ack_received", "task_id": REF}
    )
    assert cr.expected == {}


def test_committed_reference_rejects_non_sha256_value():
    with pytest.raises(ValidationError):
        CommittedReference.model_validate(
            {
                "criterion": "artifact_match",
                "task_id": REF,
                "expected": {"0": "deadbeef"},
            }
        )


def test_committed_reference_rejects_uppercase_sha256_value():
    with pytest.raises(ValidationError):
        CommittedReference.model_validate(
            {
                "criterion": "artifact_match",
                "task_id": REF,
                "expected": {"0": SHA0.upper()},
            }
        )


def test_committed_reference_rejects_non_int_seq_key():
    with pytest.raises(ValidationError):
        CommittedReference.model_validate(
            {"criterion": "artifact_match", "task_id": REF, "expected": {"abc": SHA0}}
        )


# --------------------------------------------------------------------------- #
# SettleOpenIn (contract S2).
# --------------------------------------------------------------------------- #


def _open_kwargs(**overrides):
    base = dict(
        ref=REF,
        rate=100,
        max_total=500,
        committed_reference=CommittedReference.model_validate(
            {"criterion": "artifact_match", "task_id": REF, "expected": {"0": SHA0}}
        ),
    )
    base.update(overrides)
    return base


def test_settle_open_valid_defaults_to_hash_only():
    m = SettleOpenIn.model_validate(_open_kwargs())
    assert m.attestor is Attestor.HASH_ONLY
    assert m.committed_reference.expected == {"0": SHA0}


def test_settle_open_accepts_signed_attestor():
    m = SettleOpenIn.model_validate(_open_kwargs(attestor="signed"))
    assert m.attestor is Attestor.SIGNED


def test_settle_open_task_id_must_equal_ref():
    cr = CommittedReference.model_validate(
        {
            "criterion": "artifact_match",
            "task_id": "not-the-ref",
            "expected": {"0": SHA0},
        }
    )
    with pytest.raises(ValidationError):
        SettleOpenIn.model_validate(_open_kwargs(committed_reference=cr))


def test_settle_open_rejects_negative_rate():
    with pytest.raises(ValidationError):
        SettleOpenIn.model_validate(_open_kwargs(rate=-1))


def test_settle_open_forbids_unknown_field():
    with pytest.raises(ValidationError):
        SettleOpenIn.model_validate(_open_kwargs(surprise="nope"))


# --------------------------------------------------------------------------- #
# UnitIn / Attestation / AdvanceIn (contract S3).
# --------------------------------------------------------------------------- #


def test_unit_valid_with_declared_checksum():
    u = UnitIn(seq=0, payload_hex="00ff", declared_checksum=SHA0)
    assert u.declared_checksum == SHA0
    assert bytes.fromhex(u.payload_hex) == b"\x00\xff"


def test_unit_declared_checksum_null_is_allowed():
    u = UnitIn(seq=0, payload_hex="00ff")
    assert u.declared_checksum is None


def test_unit_rejects_non_hex_payload():
    with pytest.raises(ValidationError):
        UnitIn(seq=0, payload_hex="zz")


def test_unit_rejects_odd_length_payload():
    with pytest.raises(ValidationError):
        UnitIn(seq=0, payload_hex="abc")


def test_unit_rejects_bad_declared_checksum():
    with pytest.raises(ValidationError):
        UnitIn(seq=0, payload_hex="00ff", declared_checksum="not-a-sha")


def test_advance_without_attestation_hash_only_shape():
    m = AdvanceIn(
        ref=REF, unit=UnitIn(seq=0, payload_hex="00ff", declared_checksum=SHA0)
    )
    assert m.attestation is None


def test_advance_with_attestation_signed_shape():
    m = AdvanceIn(
        ref=REF,
        unit=UnitIn(seq=0, payload_hex="00ff", declared_checksum=SHA0),
        attestation=Attestation(signer_pubkey="pk", sig="sig"),
    )
    assert m.attestation is not None
    assert m.attestation.signer_pubkey == "pk"


# --------------------------------------------------------------------------- #
# OUT models (contract S4) + Receipt (S9).
# --------------------------------------------------------------------------- #


def test_per_unit_verdict_parses_pass_and_fail():
    assert (
        PerUnit.model_validate({"seq": 0, "verdict": "pass", "reason": "ok"}).verdict
        is Verdict.PASS
    )
    assert (
        PerUnit.model_validate(
            {"seq": 1, "verdict": "fail", "reason": "mismatch"}
        ).verdict
        is Verdict.FAIL
    )


def test_per_unit_rejects_unknown_verdict():
    with pytest.raises(ValidationError):
        PerUnit.model_validate({"seq": 0, "verdict": "maybe", "reason": "?"})


def test_settle_out_defaults_signature_null_and_serializes_verdict_as_string():
    out = SettleOut(
        per_unit=[
            PerUnit.model_validate({"seq": 0, "verdict": "pass", "reason": "ok"})
        ],
        settled_total=100,
        remainder_unspent=400,
        trace=["stream-open:...", "gate:...:0:pass"],
        display="TRANSCRIPT",
    )
    assert out.verdict_signature is None
    dumped = out.model_dump(mode="json")
    assert dumped["per_unit"][0]["verdict"] == "pass"


def test_receipt_canonical_bytes_is_deterministic_and_matches_helper():
    r = Receipt(
        ref=REF,
        per_unit=[
            PerUnit.model_validate({"seq": 0, "verdict": "pass", "reason": "ok"})
        ],
        settled_total=100,
        remainder_unspent=400,
    )
    assert r.canonical_bytes() == r.canonical_bytes()
    assert r.canonical_bytes() == canonical_bytes(r.model_dump(mode="json"))
    # verdict is encoded by its wire value, not the enum name.
    assert b'"verdict":"pass"' in r.canonical_bytes()


# --------------------------------------------------------------------------- #
# The money invariant: canonical rule ties commitment <-> delivery (good path).
# --------------------------------------------------------------------------- #


def test_good_path_hash_ties_commitment_to_delivery():
    # A conforming artifact, hashed with the frozen rule, is what the buyer commits to.
    unit_obj = {"seq": 0, "task_id": REF, "kind": "invoice", "amount": 500}
    chunk = canonical_bytes(unit_obj)
    committed = committed_hash(unit_obj)

    # committed hash == sha256 of the exact delivered chunk (contract S6 good path).
    assert hashlib.sha256(chunk).hexdigest() == committed
    # ref is embedded in the payload, which is what L3's task_id-in-payload check needs.
    assert REF.encode() in chunk

    open_in = SettleOpenIn(
        ref=REF,
        rate=100,
        max_total=500,
        committed_reference=CommittedReference.model_validate(
            {
                "criterion": "artifact_match",
                "task_id": REF,
                "expected": {"0": committed},
            }
        ),
    )
    unit = UnitIn(seq=0, payload_hex=chunk.hex(), declared_checksum=committed)

    # The delivered chunk reproduces the committed hash -> L3 identity will hold,
    # and the seller's declared checksum equals it -> L2 will settle.
    expected_for_seq0 = open_in.committed_reference.expected["0"]
    assert (
        hashlib.sha256(bytes.fromhex(unit.payload_hex)).hexdigest() == expected_for_seq0
    )
    assert unit.declared_checksum == expected_for_seq0
