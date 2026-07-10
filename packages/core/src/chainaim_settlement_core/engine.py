# SPDX-License-Identifier: Apache-2.0
"""The per-tick release engine: one settlement session (one ``ref``).

Drives the FROZEN PR #61 ``OutcomeVerifiedSettlement`` plugin (imported, never edited) with
the L1/L2/L3 ladder from :mod:`.gates`, emitting the S7 trace as it goes. The plugin is
async and meters purely by tick; the gating decision -- whether a unit may settle at all --
lives here.

Lifecycle mirrors the contract endpoints: ``open`` (POST /settle/open) -> ``advance`` per
unit (POST /settle/advance) -> ``close`` (POST /settle/close). ``advance`` and ``close``
return the cumulative :class:`SettleOut`.

Per-unit release rule (this is the whole point): run the ladder; on PASS advance the meter
by one tick, releasing ``rate`` (bounded by the remaining cap); on FAIL close the stream
immediately so the remainder is never spent. The decision precedes the drain, so a failed
unit never bills -- the invariant ``billed <= rate * verified`` holds and is reconstructable
from the trace.

Attestor tiers (contract S9) are enforced by ``attestor.py`` (Group D) BEFORE release; this
engine is deliberately tier-agnostic and does not itself verify the delivery attestation. It
never fakes a signature check.
"""

from __future__ import annotations

import hashlib

from nest_core.scenarios_builtin.gates import UnitContext
from nest_core.types import AgentId, PaymentRef
from nest_plugins_reference.payments.outcome_verified_settlement import (
    OutcomeVerifiedSettlement,
)

from . import trace as trace_lines
from .contract import (
    AdvanceIn,
    Criterion,
    PerUnit,
    Receipt,
    SettleOpenIn,
    SettleOut,
    Verdict,
)
from .gates import DEFAULT_ALGO, evaluate_unit

DEFAULT_PAYER = "buyer"
DEFAULT_PAYEE = "seller"


class SettlementEngine:
    """One outcome-verified settlement session, keyed by a single deal ``ref``.

    The HTTP shell (Group E) keeps one engine per open ``ref``. Construct with optional
    ``payer``/``payee`` identities (they appear in the trace) and an L2 ``algo`` flag.

    Example::

        eng = SettlementEngine(payer="buyer", payee="seller", algo="sha256")
        await eng.open(open_in)
        out = await eng.advance(advance_in)
        out = await eng.close()
    """

    def __init__(
        self,
        *,
        payer: str = DEFAULT_PAYER,
        payee: str = DEFAULT_PAYEE,
        algo: str = DEFAULT_ALGO,
    ) -> None:
        # Validate the digest algorithm once, at construction, like the fork's ChecksumGate
        # -- a bad --algo fails fast at open, not per unit at decision time.
        if algo not in hashlib.algorithms_available:
            raise ValueError(f"unknown hash algorithm {algo!r}")
        self._payer = AgentId(payer)
        self._payee = AgentId(payee)
        self._algo = algo

        self._open_in: SettleOpenIn | None = None
        self._plugin: OutcomeVerifiedSettlement | None = None
        self._ref: PaymentRef | None = None

        self._tick = 0
        self._settled = 0
        self._closed = False
        self._per_unit: list[PerUnit] = []
        self._trace: list[str] = []
        self._seen_seqs: set[int] = set()

    # -- lifecycle ----------------------------------------------------------

    async def open(self, open_in: SettleOpenIn) -> SettleOut:
        """Open the stream (POST /settle/open). Idempotent-unsafe: call once per engine."""
        if self._open_in is not None:
            raise RuntimeError("settlement already opened on this engine")
        # The fork plugin requires rate_per_tick > 0 and max_total > 0. The frozen contract
        # permits ge=0; guard here with a clear domain error rather than leak the fork's
        # ValueError or re-open Group B's frozen-green model.
        if open_in.rate < 1:
            raise ValueError("rate must be >= 1 (amount released per verified unit)")
        if open_in.max_total < 1:
            raise ValueError("max_total must be >= 1 (spending cap)")

        plugin = OutcomeVerifiedSettlement(
            self._payer, initial_balance=open_in.max_total
        )
        ref = PaymentRef(open_in.ref)
        await plugin.open_stream(
            self._payee,
            rate_per_tick=open_in.rate,
            max_total=open_in.max_total,
            ref=ref,
            opened_at_tick=0,
        )

        self._open_in = open_in
        self._plugin = plugin
        self._ref = ref
        self._trace.append(
            trace_lines.stream_open(
                open_in.ref,
                self._payer,
                self._payee,
                open_in.rate,
                open_in.max_total,
                0,
            )
        )
        return self._snapshot()

    async def advance(self, adv: AdvanceIn) -> SettleOut:
        """Process ONE delivered unit (POST /settle/advance) and return the cumulative out.

        Runs the ladder, then releases (pass) or closes (fail). A closed session is a no-op.
        """
        open_in, plugin, ref = self._require_open()
        if adv.ref != open_in.ref:
            raise ValueError(f"ref mismatch: {adv.ref!r} != opened {open_in.ref!r}")

        # A closed stream accepts no further units (contract: close is terminal).
        if self._closed:
            return self._snapshot()

        unit = adv.unit
        seq = unit.seq
        if seq in self._seen_seqs:
            raise ValueError(f"duplicate unit seq: {seq}")
        self._seen_seqs.add(seq)

        criterion = open_in.committed_reference.criterion
        chunk = bytes.fromhex(unit.payload_hex)
        ctx = UnitContext(
            ref=open_in.ref,
            seq=seq,
            ack_received=True,  # the unit was delivered to /advance, so it arrived
            chunk=chunk,
            declared_checksum=unit.declared_checksum,
        )

        # ack line: L1 form for ack_received, content form (with bytes + checksum) otherwise.
        if criterion is Criterion.ACK_RECEIVED:
            self._trace.append(trace_lines.ack_l1(open_in.ref, seq))
        else:
            self._trace.append(
                trace_lines.ack_content(
                    open_in.ref, seq, unit.payload_hex, unit.declared_checksum
                )
            )

        expected_sha256 = (
            open_in.committed_reference.expected.get(str(seq))
            if criterion is Criterion.ARTIFACT_MATCH
            else None
        )
        result = evaluate_unit(
            ctx,
            criterion=criterion,
            expected_sha256=expected_sha256,
            task_id=open_in.ref,  # contract S5: the L3 identity check always binds task_id=ref
            algo=self._algo,
        )

        # gate line only for content criteria (S7 reserves gate for the content-gate verdict).
        if criterion is not Criterion.ACK_RECEIVED:
            self._trace.append(trace_lines.gate(open_in.ref, seq, result.passed))

        self._per_unit.append(
            PerUnit(
                seq=seq,
                verdict=Verdict.PASS if result.passed else Verdict.FAIL,
                reason=result.reason,
            )
        )

        if result.passed:
            remaining = open_in.max_total - self._settled
            if remaining <= 0:
                # Verified, but the cap is already exhausted -> nothing left to release.
                # Close deterministically; the unit's verdict still records the pass.
                await self._close_plugin(reason="cap-reached", seq=seq)
            else:
                self._tick += 1
                drained = await plugin.advance(ref, now_tick=self._tick)
                self._settled += drained
                self._trace.append(
                    trace_lines.tick(open_in.ref, seq, drained, self._tick)
                )
        else:
            # A failed unit never drains; close now so the remainder stays unspent.
            await self._close_plugin(reason=result.reason, seq=seq)

        return self._snapshot()

    async def close(self, ref: str | None = None) -> SettleOut:
        """Close the stream (POST /settle/close), freezing the billed total. Idempotent."""
        open_in, _plugin, _ref = self._require_open()
        if ref is not None and ref != open_in.ref:
            raise ValueError(f"ref mismatch: {ref!r} != opened {open_in.ref!r}")
        if not self._closed:
            last_seq = self._per_unit[-1].seq if self._per_unit else 0
            await self._close_plugin(reason="closed", seq=last_seq)
        return self._snapshot()

    # -- signable receipt (consumed by Group D signing) ---------------------

    def receipt(self) -> Receipt:
        """The signable settlement core (ref + verdicts + totals) for Group D to sign."""
        open_in, _plugin, _ref = self._require_open()
        return Receipt(
            ref=open_in.ref,
            per_unit=list(self._per_unit),
            settled_total=self._settled,
            remainder_unspent=open_in.max_total - self._settled,
        )

    # -- internals ----------------------------------------------------------

    def _require_open(
        self,
    ) -> tuple[SettleOpenIn, OutcomeVerifiedSettlement, PaymentRef]:
        if self._open_in is None or self._plugin is None or self._ref is None:
            raise RuntimeError("settlement not opened; call open() first")
        return self._open_in, self._plugin, self._ref

    async def _close_plugin(self, *, reason: str, seq: int) -> None:
        open_in, plugin, ref = self._require_open()
        if self._closed:
            return
        receipt = await plugin.close_stream(ref, now_tick=self._tick)
        # Reconcile our running total with the plugin's own frozen receipt (defensive).
        self._settled = receipt.amount.amount
        self._closed = True
        self._trace.append(
            trace_lines.stream_close(
                open_in.ref, seq, self._settled, self._tick, reason
            )
        )

    def _snapshot(self) -> SettleOut:
        open_in, _plugin, _ref = self._require_open()
        remainder = open_in.max_total - self._settled
        return SettleOut(
            per_unit=list(self._per_unit),
            settled_total=self._settled,
            remainder_unspent=remainder,
            trace=list(self._trace),
            verdict_signature=None,  # hash-only tier; signed tier fills this in Group D
            display=self._render_display(remainder),
        )

    def _render_display(self, remainder: int) -> str:
        # Human-readable transcript. This shapes ONLY the printed `display` string; the
        # machine-readable tokens are unchanged in PerUnit.reason and in every trace line,
        # so downstream verification (the trace and the signed receipt) is untouched.
        open_in = self._open_in
        assert open_in is not None  # _snapshot only calls after _require_open
        plain_reason = {
            "artifact-match": "genuine, matched what was promised",
            "checksum-ok": "intact, checksum matched",
            "ack": "delivered",
            "artifact-mismatch": "did not match what was promised",
            "checksum-mismatch": "corrupted, checksum did not match",
            "checksum-null": "no checksum was provided",
            "no-ack": "never arrived",
            "no-expected-hash": "could not be verified (no committed hash)",
            "no-task-id": "could not be verified (deal id missing from payload)",
        }
        plain_rule = {
            Criterion.ARTIFACT_MATCH: "only if it exactly matches what was promised",
            Criterion.CHECKSUM: "only if it arrives intact (checksum matches)",
            Criterion.ACK_RECEIVED: "once it has been delivered",
        }
        done = self._closed
        rule = plain_rule.get(
            open_in.committed_reference.criterion, "if it passes verification"
        )
        header = (
            f"Settlement {'complete' if done else 'in progress'}: deal {open_in.ref}"
        )
        subhead = f"Rule: pay {open_in.rate} per delivered item, {rule}."
        rows: list[str] = []
        for pu in self._per_unit:
            plain = plain_reason.get(pu.reason, pu.reason)
            if pu.verdict is Verdict.PASS:
                rows.append(f"  Item {pu.seq}: PAID - {plain}.")
            else:
                rows.append(
                    f"  Item {pu.seq}: NOT PAID - {plain}; payment stopped here."
                )
        status = "FINISHED" if done else "IN PROGRESS"
        footer = (
            f"Total paid: {self._settled} | "
            f"Budget unspent: {remainder} | Status: {status}"
        )
        return "\n".join([header, subhead, *rows, footer])
