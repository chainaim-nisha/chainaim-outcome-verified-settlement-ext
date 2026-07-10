"""Group C tests -- the per-tick release engine (:mod:`chainaim_settlement_core.engine`).

Covers the build-plan targets: the ``billed <= rate * verified`` invariant (reconciled from
the trace, not the plugin's accounting), remainder-unspent on the two failure modes, and a
deterministic close. The engine is async (it drives the fork's async plugin); tests drive it
with ``asyncio.run`` so no ``pytest-asyncio`` dependency is needed.
"""

import asyncio
import hashlib

import pytest

from chainaim_settlement_core.contract import (
    AdvanceIn,
    CommittedReference,
    SettleOpenIn,
    UnitIn,
    Verdict,
    canonical_bytes,
)
from chainaim_settlement_core.engine import SettlementEngine

REF = hashlib.sha256(b"deal-engine").hexdigest()


def _artifact(seq: int) -> tuple[str, str]:
    """Return (payload_hex, sha256_hex) for a conforming unit that embeds task_id=REF."""
    chunk = canonical_bytes({"seq": seq, "task_id": REF, "body": f"invoice-{seq}"})
    return chunk.hex(), hashlib.sha256(chunk).hexdigest()


def _expected(seqs: list[int]) -> dict[str, str]:
    return {str(s): _artifact(s)[1] for s in seqs}


def _open_in(
    expected: dict[str, str], *, rate: int = 100, max_total: int = 500
) -> SettleOpenIn:
    return SettleOpenIn(
        ref=REF,
        rate=rate,
        max_total=max_total,
        committed_reference=CommittedReference.model_validate(
            {"criterion": "artifact_match", "task_id": REF, "expected": expected}
        ),
    )


def _good_unit(seq: int) -> AdvanceIn:
    payload_hex, sha = _artifact(seq)
    return AdvanceIn(
        ref=REF, unit=UnitIn(seq=seq, payload_hex=payload_hex, declared_checksum=sha)
    )


def _corrupt(payload_hex: str) -> str:
    b = bytearray(bytes.fromhex(payload_hex))
    b[0] ^= 0xFF
    return b.hex()


def _billed_from_trace(trace: list[str]) -> int:
    """Sum the released amounts from tick lines -- the validators' billed reconstruction."""
    total = 0
    for line in trace:
        if line.startswith("tick:"):
            total += int(line.split(":")[3])  # tick:<ref>:<seq>:<amount>:<now_tick>
    return total


# --------------------------------------------------------------------------- #
# Scenario runners.
# --------------------------------------------------------------------------- #


async def _run_good():
    eng = SettlementEngine()
    await eng.open(_open_in(_expected([0, 1, 2])))
    outs = [await eng.advance(_good_unit(s)) for s in (0, 1, 2)]
    final = await eng.close()
    return eng, outs, final


async def _run_corrupted():
    eng = SettlementEngine()
    await eng.open(_open_in(_expected([0, 1])))
    o0 = await eng.advance(_good_unit(0))
    ph1, sha1 = _artifact(1)
    # Bytes mutated in transit, but the declared checksum is the (now stale) original -> L2 fail.
    o1 = await eng.advance(
        AdvanceIn(
            ref=REF,
            unit=UnitIn(seq=1, payload_hex=_corrupt(ph1), declared_checksum=sha1),
        )
    )
    return eng, o0, o1


async def _run_swapped():
    eng = SettlementEngine()
    await eng.open(_open_in(_expected([0, 1])))
    o0 = await eng.advance(_good_unit(0))
    ph99, sha99 = _artifact(99)  # a different valid artifact, honestly checksummed
    o1 = await eng.advance(
        AdvanceIn(
            ref=REF, unit=UnitIn(seq=1, payload_hex=ph99, declared_checksum=sha99)
        )
    )
    return eng, o0, o1


# --------------------------------------------------------------------------- #
# Good path -- settles per tick.
# --------------------------------------------------------------------------- #


def test_good_path_settles_each_unit():
    _eng, _outs, final = asyncio.run(_run_good())
    assert [pu.verdict for pu in final.per_unit] == [Verdict.PASS] * 3
    assert [pu.reason for pu in final.per_unit] == ["artifact-match"] * 3
    assert final.settled_total == 300
    assert final.remainder_unspent == 200


def test_good_path_billed_reconciles_from_trace():
    _eng, _outs, final = asyncio.run(_run_good())
    assert _billed_from_trace(final.trace) == final.settled_total == 300


def test_invariant_billed_le_rate_times_verified():
    _eng, _outs, final = asyncio.run(_run_good())
    verified = sum(1 for pu in final.per_unit if pu.verdict is Verdict.PASS)
    assert final.settled_total <= 100 * verified


def test_good_path_advance_returns_growing_settled():
    _eng, outs, _final = asyncio.run(_run_good())
    assert [o.settled_total for o in outs] == [100, 200, 300]


# --------------------------------------------------------------------------- #
# Failure modes -- close, remainder unspent.
# --------------------------------------------------------------------------- #


def test_corrupted_unit_fails_l2_and_closes_remainder_unspent():
    _eng, _o0, o1 = asyncio.run(_run_corrupted())
    assert o1.per_unit[0].verdict is Verdict.PASS
    assert o1.per_unit[1].verdict is Verdict.FAIL
    assert o1.per_unit[1].reason == "checksum-mismatch"
    assert o1.settled_total == 100
    assert o1.remainder_unspent == 400
    assert any(
        line.startswith("stream-close:") and line.endswith(":checksum-mismatch")
        for line in o1.trace
    )


def test_swapped_honest_checksum_fails_l3_and_closes():
    _eng, _o0, o1 = asyncio.run(_run_swapped())
    assert o1.per_unit[1].verdict is Verdict.FAIL
    assert o1.per_unit[1].reason == "artifact-mismatch"
    assert o1.settled_total == 100
    assert o1.remainder_unspent == 400
    assert any(
        line.startswith("stream-close:") and line.endswith(":artifact-mismatch")
        for line in o1.trace
    )


def test_advance_after_close_is_noop():
    async def run():
        eng = SettlementEngine()
        await eng.open(_open_in(_expected([0, 1])))
        ph0, sha0 = _artifact(0)
        # Fail unit 0 -> stream closes.
        await eng.advance(
            AdvanceIn(
                ref=REF,
                unit=UnitIn(seq=0, payload_hex=_corrupt(ph0), declared_checksum=sha0),
            )
        )
        closed_trace = (await eng.close()).trace
        after = await eng.advance(_good_unit(1))
        return closed_trace, after

    closed_trace, after = asyncio.run(run())
    assert len(after.per_unit) == 1  # unit 1 was never processed
    assert after.trace == closed_trace  # no new lines


# --------------------------------------------------------------------------- #
# Determinism + close semantics.
# --------------------------------------------------------------------------- #


def test_close_is_deterministic_across_runs():
    _e1, _o1, f1 = asyncio.run(_run_good())
    _e2, _o2, f2 = asyncio.run(_run_good())
    assert f1.trace == f2.trace
    assert f1.settled_total == f2.settled_total
    assert [(p.seq, p.verdict, p.reason) for p in f1.per_unit] == [
        (p.seq, p.verdict, p.reason) for p in f2.per_unit
    ]


def test_close_is_idempotent():
    async def run():
        eng = SettlementEngine()
        await eng.open(_open_in(_expected([0])))
        await eng.advance(_good_unit(0))
        c1 = await eng.close()
        c2 = await eng.close()
        return c1, c2

    c1, c2 = asyncio.run(run())
    assert c1.trace == c2.trace
    assert c1.settled_total == c2.settled_total == 100
    assert sum(1 for line in c2.trace if line.startswith("stream-close:")) == 1


# --------------------------------------------------------------------------- #
# ack_received criterion (L1 only) + guards.
# --------------------------------------------------------------------------- #


def test_ack_received_criterion_settles_on_arrival_without_gate_line():
    async def run():
        eng = SettlementEngine()
        await eng.open(
            SettleOpenIn(
                ref=REF,
                rate=50,
                max_total=100,
                committed_reference=CommittedReference.model_validate(
                    {"criterion": "ack_received", "task_id": REF, "expected": {}}
                ),
            )
        )
        return await eng.advance(
            AdvanceIn(ref=REF, unit=UnitIn(seq=0, payload_hex="00ff"))
        )

    out = asyncio.run(run())
    assert out.per_unit[0].verdict is Verdict.PASS
    assert out.per_unit[0].reason == "ack"
    assert out.settled_total == 50
    assert f"ack:{REF}:0" in out.trace
    assert not any(line.startswith(f"gate:{REF}:") for line in out.trace)


def test_open_rejects_zero_rate():
    async def run():
        eng = SettlementEngine()
        await eng.open(
            SettleOpenIn(
                ref=REF,
                rate=0,
                max_total=500,
                committed_reference=CommittedReference.model_validate(
                    {"criterion": "checksum", "task_id": REF, "expected": {}}
                ),
            )
        )

    with pytest.raises(ValueError):
        asyncio.run(run())


def test_constructor_rejects_bad_algo():
    with pytest.raises(ValueError):
        SettlementEngine(algo="not-a-real-algo")


def test_duplicate_seq_rejected():
    async def run():
        eng = SettlementEngine()
        await eng.open(_open_in(_expected([0])))
        await eng.advance(_good_unit(0))
        await eng.advance(_good_unit(0))

    with pytest.raises(ValueError):
        asyncio.run(run())


def test_advance_ref_mismatch_rejected():
    async def run():
        eng = SettlementEngine()
        await eng.open(_open_in(_expected([0])))
        ph0, sha0 = _artifact(0)
        await eng.advance(
            AdvanceIn(
                ref="different-ref",
                unit=UnitIn(seq=0, payload_hex=ph0, declared_checksum=sha0),
            )
        )

    with pytest.raises(ValueError):
        asyncio.run(run())


# --------------------------------------------------------------------------- #
# Signable receipt seam (consumed by Group D).
# --------------------------------------------------------------------------- #


def test_receipt_matches_settlement_state():
    eng, _outs, final = asyncio.run(_run_good())
    r = eng.receipt()
    assert r.ref == REF
    assert r.settled_total == final.settled_total == 300
    assert r.remainder_unspent == final.remainder_unspent == 200
    assert [pu.verdict for pu in r.per_unit] == [Verdict.PASS] * 3
    assert r.canonical_bytes() == r.canonical_bytes()  # deterministic signable bytes
