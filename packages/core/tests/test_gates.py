"""Group C tests -- the L1/L2/L3 gate ladder (:mod:`chainaim_settlement_core.gates`).

Covers what the build plan names: artifact_match match/mismatch, L2 checksum, L1 ack,
null-checksum-never-settles, and the swapped-honest-checksum case that passes L2 but fails
L3. Uses the imported ``UnitContext`` from the fork so we test the real seam, and the frozen
``canonical_bytes`` so the chunks are produced the same way the builder produces them.
"""

import hashlib

import pytest
from nest_core.scenarios_builtin.gates import UnitContext

from chainaim_settlement_core.contract import Criterion, canonical_bytes
from chainaim_settlement_core.gates import (
    GateResult,
    digest_hex,
    evaluate_unit,
    l1_ack,
    l2_checksum,
    l3_artifact_match,
)

REF = hashlib.sha256(b"deal-gate").hexdigest()


def _conforming_chunk(seq: int) -> bytes:
    # Embeds task_id=REF so the L3 task_id-in-payload check is satisfied for the good path.
    return canonical_bytes({"seq": seq, "task_id": REF, "body": f"invoice-{seq}"})


def _sha(chunk: bytes) -> str:
    return hashlib.sha256(chunk).hexdigest()


# --------------------------------------------------------------------------- #
# digest_hex.
# --------------------------------------------------------------------------- #


def test_digest_hex_is_sha256_by_default():
    assert digest_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_digest_hex_rejects_unknown_algo():
    with pytest.raises(ValueError):
        digest_hex(b"abc", algo="not-a-real-algo")


# --------------------------------------------------------------------------- #
# L1 -- ack.
# --------------------------------------------------------------------------- #


def test_l1_ack_true_when_arrived():
    assert l1_ack(UnitContext(ref=REF, seq=0, ack_received=True)) is True


def test_l1_ack_false_when_not_arrived():
    assert l1_ack(UnitContext(ref=REF, seq=0, ack_received=False)) is False


def test_evaluate_ack_received_passes_on_arrival():
    ctx = UnitContext(ref=REF, seq=0, ack_received=True)
    assert evaluate_unit(ctx, criterion=Criterion.ACK_RECEIVED) == GateResult(
        True, "ack"
    )


def test_evaluate_no_ack_fails():
    ctx = UnitContext(ref=REF, seq=0, ack_received=False)
    assert evaluate_unit(ctx, criterion=Criterion.ACK_RECEIVED) == GateResult(
        False, "no-ack"
    )


# --------------------------------------------------------------------------- #
# L2 -- checksum.
# --------------------------------------------------------------------------- #


def test_l2_checksum_matches():
    chunk = _conforming_chunk(0)
    ctx = UnitContext(ref=REF, seq=0, chunk=chunk, declared_checksum=_sha(chunk))
    assert l2_checksum(ctx) is True


def test_l2_checksum_mismatch():
    chunk = _conforming_chunk(0)
    ctx = UnitContext(
        ref=REF, seq=0, chunk=chunk, declared_checksum=_sha(b"other-bytes")
    )
    assert l2_checksum(ctx) is False


def test_l2_null_checksum_never_settles():
    chunk = _conforming_chunk(0)
    ctx = UnitContext(ref=REF, seq=0, chunk=chunk, declared_checksum=None)
    assert l2_checksum(ctx) is False


def test_evaluate_checksum_pass():
    chunk = _conforming_chunk(0)
    ctx = UnitContext(
        ref=REF, seq=0, ack_received=True, chunk=chunk, declared_checksum=_sha(chunk)
    )
    assert evaluate_unit(ctx, criterion=Criterion.CHECKSUM) == GateResult(
        True, "checksum-ok"
    )


def test_evaluate_checksum_mismatch():
    chunk = _conforming_chunk(0)
    ctx = UnitContext(
        ref=REF, seq=0, ack_received=True, chunk=chunk, declared_checksum=_sha(b"wrong")
    )
    assert evaluate_unit(ctx, criterion=Criterion.CHECKSUM) == GateResult(
        False, "checksum-mismatch"
    )


def test_evaluate_checksum_null():
    chunk = _conforming_chunk(0)
    ctx = UnitContext(
        ref=REF, seq=0, ack_received=True, chunk=chunk, declared_checksum=None
    )
    assert evaluate_unit(ctx, criterion=Criterion.CHECKSUM) == GateResult(
        False, "checksum-null"
    )


# --------------------------------------------------------------------------- #
# L3 -- artifact_match (identity), and L2 short-circuit.
# --------------------------------------------------------------------------- #


def test_l3_helper_direct_match():
    chunk = _conforming_chunk(0)
    ctx = UnitContext(ref=REF, seq=0, chunk=chunk)
    assert l3_artifact_match(ctx, expected_sha256=_sha(chunk), task_id=REF) is True


def test_l3_helper_direct_mismatch():
    chunk = _conforming_chunk(0)
    ctx = UnitContext(ref=REF, seq=0, chunk=chunk)
    assert l3_artifact_match(ctx, expected_sha256=_sha(b"other"), task_id=REF) is False


def test_evaluate_artifact_match_pass():
    chunk = _conforming_chunk(0)
    ctx = UnitContext(
        ref=REF, seq=0, ack_received=True, chunk=chunk, declared_checksum=_sha(chunk)
    )
    result = evaluate_unit(
        ctx,
        criterion=Criterion.ARTIFACT_MATCH,
        expected_sha256=_sha(chunk),
        task_id=REF,
    )
    assert result == GateResult(True, "artifact-match")


def test_evaluate_artifact_swapped_honest_checksum_fails_l3():
    # The case L2 alone cannot catch: the seller delivers a DIFFERENT valid artifact with an
    # HONEST checksum for those bytes (L2 passes), but its hash != the buyer's committed hash.
    delivered = _conforming_chunk(99)
    committed_for_slot = _conforming_chunk(0)
    ctx = UnitContext(
        ref=REF,
        seq=0,
        ack_received=True,
        chunk=delivered,
        declared_checksum=_sha(delivered),
    )
    result = evaluate_unit(
        ctx,
        criterion=Criterion.ARTIFACT_MATCH,
        expected_sha256=_sha(committed_for_slot),
        task_id=REF,
    )
    assert result == GateResult(False, "artifact-mismatch")


def test_evaluate_artifact_l2_failure_short_circuits_l3():
    # A wrong declared checksum fails L2; L3 must never run, so the reason is a checksum one
    # even though expected_sha256 would have matched the delivered bytes.
    chunk = _conforming_chunk(0)
    ctx = UnitContext(
        ref=REF, seq=0, ack_received=True, chunk=chunk, declared_checksum=_sha(b"stale")
    )
    result = evaluate_unit(
        ctx,
        criterion=Criterion.ARTIFACT_MATCH,
        expected_sha256=_sha(chunk),
        task_id=REF,
    )
    assert result == GateResult(False, "checksum-mismatch")


def test_evaluate_artifact_missing_expected_hash():
    chunk = _conforming_chunk(0)
    ctx = UnitContext(
        ref=REF, seq=0, ack_received=True, chunk=chunk, declared_checksum=_sha(chunk)
    )
    result = evaluate_unit(
        ctx, criterion=Criterion.ARTIFACT_MATCH, expected_sha256=None, task_id=REF
    )
    assert result == GateResult(False, "no-expected-hash")


def test_evaluate_artifact_task_id_not_in_payload_fails():
    # Hash matches, but the committed task_id (ref) is absent from the delivered bytes.
    chunk = canonical_bytes({"seq": 0, "body": "no-ref-embedded"})
    ctx = UnitContext(
        ref=REF, seq=0, ack_received=True, chunk=chunk, declared_checksum=_sha(chunk)
    )
    result = evaluate_unit(
        ctx,
        criterion=Criterion.ARTIFACT_MATCH,
        expected_sha256=_sha(chunk),
        task_id=REF,
    )
    assert result == GateResult(False, "artifact-mismatch")
