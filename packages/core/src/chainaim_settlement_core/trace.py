# SPDX-License-Identifier: Apache-2.0
"""Trace-line emitters for the contract S7 grammar.

Each function returns ONE formatted line; the engine appends them to ``SettleOut.trace``.
The four adversarial validators reconcile billed-vs-verified FROM THIS TRACE, not from the
plugin's own accounting, so the grammar is load-bearing and frozen.

Grammar (contract S7)::

    stream-open:<ref>:<payer>:<payee>:<rate>:<max_total>:<opened_tick>
    tick:<ref>:<seq>:<amount>:<now_tick>              amount released at this tick
    ack:<ref>:<seq>                                   L1 (ack_received criterion)
    ack:<ref>:<seq>:<chunk_hex>:<declared_checksum>   content gate (checksum / artifact_match)
    gate:<ref>:<seq>:pass|fail                        content-gate verdict
    stream-close:<ref>:<seq>:<drained>:<close_tick>:<reason>

Separator is ``:``. Every embedded value is colon-free -- ``ref`` and ``declared_checksum``
are sha256 hex, ``chunk_hex`` is hex, ``payer``/``payee`` are bare ids, ``reason`` is a
single token -- so a validator can split on ``:`` from the left unambiguously.
"""

from __future__ import annotations


def stream_open(
    ref: str, payer: str, payee: str, rate: int, max_total: int, opened_tick: int
) -> str:
    """Emitted once at open, before any unit is processed."""
    return f"stream-open:{ref}:{payer}:{payee}:{rate}:{max_total}:{opened_tick}"


def tick(ref: str, seq: int, amount: int, now_tick: int) -> str:
    """Emitted when a passed unit releases funds. ``amount`` is what was actually released
    this tick (equals the configured rate on the normal path; less only when the cap binds),
    so summing tick amounts reconstructs the billed total exactly."""
    return f"tick:{ref}:{seq}:{amount}:{now_tick}"


def ack_l1(ref: str, seq: int) -> str:
    """L1-only ack line (criterion=ack_received): the unit arrived."""
    return f"ack:{ref}:{seq}"


def ack_content(
    ref: str, seq: int, chunk_hex: str, declared_checksum: str | None
) -> str:
    """Content-gate ack line (criterion=checksum/artifact_match): unit arrived with content.

    A null declared checksum renders as an empty final field (``...:<chunk_hex>:``)."""
    declared = declared_checksum if declared_checksum is not None else ""
    return f"ack:{ref}:{seq}:{chunk_hex}:{declared}"


def gate(ref: str, seq: int, passed: bool) -> str:
    """Content-gate verdict line."""
    return f"gate:{ref}:{seq}:{'pass' if passed else 'fail'}"


def stream_close(ref: str, seq: int, drained: int, close_tick: int, reason: str) -> str:
    """Emitted once when the stream closes (on a failed unit, a reached cap, or an explicit
    close). ``drained`` is the frozen billed total; the remainder is never spent."""
    return f"stream-close:{ref}:{seq}:{drained}:{close_tick}:{reason}"
