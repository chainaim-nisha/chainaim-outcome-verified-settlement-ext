# SPDX-License-Identifier: Apache-2.0
"""Self-contained settlement smoke demo (Group F) -- no HTTP layer, no network.

Drives the async :class:`SettlementEngine` in-process over the static committed fixtures in
``demo_data/`` and prints the ``display`` transcript plus the S7 trace, so nisha's
settlement-verifier can be demonstrated stand-alone (no sathya, no running server). Two
money-shot cases:

    good : every delivered unit matches the committed reference -> settles per tick.
    bad  : one unit matches (settles a tick), the next is a valid-but-different artifact with
           an honest checksum -> passes L2, fails L3 identity -> the stream closes and the
           remainder is never spent. (--fail-mode l2 instead corrupts the delivered bytes so
           the checksum gate itself fails.)

The engine closes on the FIRST failed unit (a failure is terminal), so one stream shows
exactly one failure -- that is the point: the remainder stays unspent from that tick on.

Usage::

    python smoke.py good
    python smoke.py bad
    python smoke.py bad --fail-mode l2
    python smoke.py good --rate 50 --max-total 150
    python smoke.py --regen        # recompute demo_data/*.json from the scenario, then exit
    python smoke.py good --json    # machine-readable SettleOut instead of the transcript

Exit code is 0 iff the run matched its expected shape (good: no failures; bad: closed early
with remainder unspent), so this doubles as a CI smoke check.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from chainaim_settlement_core.contract import (
    AdvanceIn,
    SettleOpenIn,
    SettleOut,
    UnitIn,
    Verdict,
    canonical_bytes,
    committed_hash,
)
from chainaim_settlement_core.engine import SettlementEngine

DEMO_DIR = Path(__file__).with_name("demo_data")
COMMITTED_PATH = DEMO_DIR / "committed_reference.json"
DELIVERY = {
    "good": DEMO_DIR / "delivery_good.json",
    "bad": DEMO_DIR / "delivery_bad.json",
}

# Scenario source of truth for --regen. REF binds committed_reference.task_id == ref.
REF = hashlib.sha256(b"smoke-settlement").hexdigest()
RATE = 100
MAX_TOTAL = 500
GOOD_BODIES = {0: "invoice-conforming-0", 1: "invoice-conforming-1"}
BAD_SWAP_BODY = (
    "a-different-artifact"  # seq 1: honest checksum, wrong identity -> L3 fail
)


def _artifact(seq: int, body: str) -> tuple[str, str]:
    """(payload_hex, sha256_hex) for a conforming chunk that embeds task_id=REF.

    The chunk is the frozen canonical encoding of the unit; its sha256 is BOTH the delivered
    declared_checksum (honest) and the committed expected[seq] for a matching unit.
    """
    obj = {"seq": seq, "task_id": REF, "body": body}
    return canonical_bytes(obj).hex(), committed_hash(obj)


def regen() -> None:
    """Recompute the three demo_data fixtures from the scenario above and write them.

    Hashes come from the frozen canonical rule, so the fixtures are always reproducible and
    never hand-authored. Safe to re-run; overwrites in place.
    """
    ph0, sha0 = _artifact(0, GOOD_BODIES[0])
    ph1, sha1 = _artifact(1, GOOD_BODIES[1])
    phx, shax = _artifact(1, BAD_SWAP_BODY)
    committed = {
        "ref": REF,
        "rate": RATE,
        "max_total": MAX_TOTAL,
        "attestor": "hash-only",
        "committed_reference": {
            "criterion": "artifact_match",
            "task_id": REF,
            "expected": {"0": sha0, "1": sha1},
        },
    }
    good = {
        "units": [
            {"seq": 0, "payload_hex": ph0, "declared_checksum": sha0},
            {"seq": 1, "payload_hex": ph1, "declared_checksum": sha1},
        ]
    }
    bad = {
        "units": [
            {"seq": 0, "payload_hex": ph0, "declared_checksum": sha0},
            {"seq": 1, "payload_hex": phx, "declared_checksum": shax},
        ]
    }
    DEMO_DIR.mkdir(exist_ok=True)
    for path, obj in (
        (COMMITTED_PATH, committed),
        (DELIVERY["good"], good),
        (DELIVERY["bad"], bad),
    ):
        path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")


def _load_open(rate: int | None, max_total: int | None) -> SettleOpenIn:
    raw = json.loads(COMMITTED_PATH.read_text(encoding="utf-8"))
    if rate is not None:
        raw["rate"] = rate
    if max_total is not None:
        raw["max_total"] = max_total
    return SettleOpenIn.model_validate(raw)


def _load_units(case: str) -> list[UnitIn]:
    raw = json.loads(DELIVERY[case].read_text(encoding="utf-8"))
    return [UnitIn.model_validate(u) for u in raw["units"]]


def _corrupt_l2(unit: UnitIn) -> UnitIn:
    """Flip one byte of the payload but keep the (now stale) declared checksum, so the L2
    constant-time compare fails (checksum-mismatch) rather than the L3 identity check."""
    chunk = bytearray(bytes.fromhex(unit.payload_hex))
    chunk[-1] ^= 0x01
    return unit.model_copy(update={"payload_hex": chunk.hex()})


def _verify_good_fixture(open_in: SettleOpenIn, units: list[UnitIn]) -> None:
    """Fail loudly if the good fixture is not self-consistent (stale / hand-edited), rather
    than letting a broken demo masquerade as a real settlement failure."""
    expected = open_in.committed_reference.expected
    for u in units:
        digest = hashlib.sha256(bytes.fromhex(u.payload_hex)).hexdigest()
        if u.declared_checksum != digest:
            raise SystemExit(
                f"fixture broken: seq {u.seq} declared_checksum != sha256(payload)"
            )
        if expected.get(str(u.seq)) != digest:
            raise SystemExit(
                f"fixture broken: seq {u.seq} not committed in expected map"
            )


async def _run(open_in: SettleOpenIn, units: list[UnitIn]) -> SettleOut:
    engine = SettlementEngine()
    await engine.open(open_in)
    for unit in units:
        await engine.advance(AdvanceIn(ref=open_in.ref, unit=unit))
    return await engine.close(open_in.ref)


def _expected_ok(
    case: str, open_in: SettleOpenIn, units: list[UnitIn], out: SettleOut
) -> bool:
    failures = [pu for pu in out.per_unit if pu.verdict is Verdict.FAIL]
    if case == "good":
        want = min(open_in.rate * len(units), open_in.max_total)
        return not failures and out.settled_total == want
    return len(failures) == 1 and out.remainder_unspent > 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Self-contained settlement smoke demo."
    )
    parser.add_argument("case", nargs="?", choices=["good", "bad"], default="good")
    parser.add_argument(
        "--rate", type=int, default=None, help="override released-per-unit rate"
    )
    parser.add_argument(
        "--max-total", type=int, default=None, help="override the spending cap"
    )
    parser.add_argument(
        "--fail-mode",
        choices=["l3", "l2"],
        default="l3",
        help="bad-case failure to show: l3 wrong-artifact (default) or l2 corrupted bytes",
    )
    parser.add_argument(
        "--regen", action="store_true", help="rewrite demo_data/*.json and exit"
    )
    parser.add_argument(
        "--json", action="store_true", help="print the SettleOut as JSON"
    )
    args = parser.parse_args(argv)

    if args.regen:
        regen()
        return 0

    if not COMMITTED_PATH.exists():
        raise SystemExit(f"missing fixtures; run: python {Path(__file__).name} --regen")

    open_in = _load_open(args.rate, args.max_total)
    if args.case == "good":
        units = _load_units("good")
        _verify_good_fixture(open_in, units)
    elif args.fail_mode == "l2":
        # Deliver the COMMITTED artifact but with mangled bytes -> the L2 checksum gate
        # catches the integrity break (distinct from l3's wrong-identity artifact).
        units = _load_units("good")
        units = [*units[:-1], _corrupt_l2(units[-1])]
    else:
        units = _load_units("bad")

    out = asyncio.run(_run(open_in, units))

    if args.json:
        print(out.model_dump_json(indent=2))
    else:
        print(out.display)
        print("\ntrace:")
        for line in out.trace:
            print(f"  {line}")

    ok = _expected_ok(args.case, open_in, units, out)
    failures = sum(1 for pu in out.per_unit if pu.verdict is Verdict.FAIL)
    print(
        f"\n[{'OK' if ok else 'UNEXPECTED'}] {args.case}: settled={out.settled_total} "
        f"remainder={out.remainder_unspent} failures={failures}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
