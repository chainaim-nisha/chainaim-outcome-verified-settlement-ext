# SPDX-License-Identifier: Apache-2.0
"""The L1 -> L2 -> L3 gate ladder for one metered unit.

Grounded in the FROZEN PR #61 ``gates.py`` (read this session), but importing only the
minimal surface the contract froze: ``UnitContext`` (the gate seam's input dataclass) and
``artifact_match`` (the L3 identity check). Everything else is reimplemented here in
stdlib so this module pulls in pydantic + stdlib only -- no gate classes, no
``Gate.from_name`` (which drops criterion kwargs), no scenario driver.

The ladder mirrors the fork's ``EvaluatorGate`` composition exactly:

* **L1 ack** -- a unit arrived (``ctx.ack_received``).
* **L2 checksum** -- ``hmac.compare_digest(digest(algo, chunk), declared_checksum)``,
  constant-time; a null declared checksum never settles. This is byte-for-byte the fork's
  ``ChecksumGate`` logic, reimplemented rather than imported.
* **L3 artifact_match** -- called DIRECTLY (the b7-test style), not via ``Gate.from_name``:
  ``artifact_match(ctx, expected_sha256=expected[str(seq)], task_id=ref)``. Requires
  ``sha256(chunk).hexdigest() == expected_sha256`` AND ``ref.encode() in chunk``.

L2 short-circuits L3: an integrity failure returns before the identity check runs, so a
unit with an honest checksum for the WRONG bytes (L2 pass) still fails L3 -- the case L2
alone cannot catch.

``criterion`` routing (contract S5): ``artifact_match`` -> L1+L2+L3 ; ``checksum`` ->
L1+L2 ; ``ack_received`` -> L1 only.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from nest_core.scenarios_builtin.gates import UnitContext, artifact_match

from .contract import Criterion

DEFAULT_ALGO = "sha256"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Outcome of the ladder for one unit: settle/withhold plus a stable reason token.

    ``reason`` is a machine-readable token (never free text) so it can appear verbatim in
    ``PerUnit.reason`` and drive the ``stream-close`` reason in the trace. Known tokens:
    ``ack``, ``no-ack``, ``checksum-null``, ``checksum-mismatch``, ``checksum-ok``,
    ``no-expected-hash``, ``no-task-id``, ``artifact-match``, ``artifact-mismatch``.
    """

    passed: bool
    reason: str


def digest_hex(chunk: bytes, *, algo: str = DEFAULT_ALGO) -> str:
    """Hex digest of ``chunk`` under ``algo`` (default sha256).

    Raises ``ValueError`` for an unknown algorithm (fail fast, like the fork's
    ``ChecksumGate``). Note L3/``expected`` is always a sha256 (fork-hardcoded); ``algo``
    only parameterizes the L2 self-consistency check via the ``--algo`` flag.
    """
    return hashlib.new(algo, chunk).hexdigest()


def l1_ack(ctx: UnitContext) -> bool:
    """L1: pass iff the unit arrived (``ack_received``)."""
    return ctx.ack_received


def l2_checksum(ctx: UnitContext, *, algo: str = DEFAULT_ALGO) -> bool:
    """L2: pass iff ``digest(algo, chunk)`` equals the declared checksum, in constant time.

    A ``None`` declared checksum never settles (contract S3/S5). This is the fork's
    ``ChecksumGate`` predicate, reimplemented in stdlib.
    """
    declared = ctx.declared_checksum
    if declared is None:
        return False
    return hmac.compare_digest(digest_hex(ctx.chunk, algo=algo), declared)


def l3_artifact_match(ctx: UnitContext, *, expected_sha256: str, task_id: str) -> bool:
    """L3: the imported ``artifact_match``, called directly with both kwargs forwarded.

    Passes iff ``sha256(chunk).hexdigest() == expected_sha256`` AND ``task_id`` is present
    in ``chunk``. We always supply both (contract S5).
    """
    return artifact_match(ctx, expected_sha256=expected_sha256, task_id=task_id)


def evaluate_unit(
    ctx: UnitContext,
    *,
    criterion: Criterion,
    expected_sha256: str | None = None,
    task_id: str | None = None,
    algo: str = DEFAULT_ALGO,
) -> GateResult:
    """Run the criterion-routed ladder for one unit and return a :class:`GateResult`.

    ``expected_sha256`` is the committed hash for this unit's seq (``expected[str(seq)]``),
    required only for ``artifact_match``. ``task_id`` is the deal ``ref`` (contract S5:
    the L3 call always uses ``task_id=ref``). ``algo`` parameterizes L2 only.
    """
    # L1 -- a unit must have arrived.
    if not l1_ack(ctx):
        return GateResult(False, "no-ack")
    if criterion is Criterion.ACK_RECEIVED:
        return GateResult(True, "ack")

    # L2 -- integrity (checksum + artifact_match criteria). Short-circuits L3 on failure.
    if ctx.declared_checksum is None:
        return GateResult(False, "checksum-null")
    if not l2_checksum(ctx, algo=algo):
        return GateResult(False, "checksum-mismatch")
    if criterion is Criterion.CHECKSUM:
        return GateResult(True, "checksum-ok")

    # L3 -- identity (artifact_match only). Reached only after L2 passed.
    if expected_sha256 is None:
        return GateResult(False, "no-expected-hash")
    if task_id is None:
        return GateResult(False, "no-task-id")
    if l3_artifact_match(ctx, expected_sha256=expected_sha256, task_id=task_id):
        return GateResult(True, "artifact-match")
    return GateResult(False, "artifact-mismatch")
