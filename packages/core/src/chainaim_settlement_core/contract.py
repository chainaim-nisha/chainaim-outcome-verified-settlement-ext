"""Phase-2 Combo -- FROZEN verifier data contract (v1.0).

Faithful pydantic realization of ``ph2-combo-1_verifier-contract-v1_2026-07-09.md``.
The wire shapes here MUST match that FROZEN doc byte-for-byte in meaning; any change
(new field, shape change, criterion semantics) is a contract version bump (v1.1+),
never a silent edit.

What lives here (Group B): the request/response models both build tracks implement
against, plus the ONE canonical-bytes rule (contract S6) that ties a committed hash to
the delivered chunk. No business vocabulary, no gate logic, no engine -- those are
Group C. No crypto -- that is Group D.

Canonical rule (contract S6, decision 3), used verbatim by both the buyer/builder and
the verifier so that ``sha256(chunk).hexdigest() == committed_reference.expected[str(seq)]``::

    json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "Criterion",
    "Attestor",
    "Verdict",
    "CommittedReference",
    "SettleOpenIn",
    "UnitIn",
    "Attestation",
    "AdvanceIn",
    "PerUnit",
    "Receipt",
    "SettleOut",
    "canonical_bytes",
    "committed_hash",
]


# --------------------------------------------------------------------------- #
# Canonical bytes rule (contract S6, decision 3) -- the ONE rule both sides use.
# Kept EXACTLY as frozen: default ensure_ascii, sorted keys, compact separators,
# UTF-8. Do not add ensure_ascii=False or indent -- that would diverge the hash
# from sathya Service B's builder and break L3 identity.
# --------------------------------------------------------------------------- #


def canonical_bytes(obj: dict[str, Any]) -> bytes:
    """Deterministic JSON encoding of ``obj``: sorted keys, compact separators, UTF-8.

    This is the frozen canonical rule. The committed hash and the delivered chunk
    bytes MUST both be produced by this exact function. No pretty-printing, no
    trailing whitespace, keys sorted (recursively), compact ``,``/``:`` separators.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def committed_hash(obj: dict[str, Any]) -> str:
    """sha256 hexdigest of ``canonical_bytes(obj)`` -- one artifact's committed hash."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


# --------------------------------------------------------------------------- #
# Enums -- the frozen string VALUES are the on-the-wire values.
# --------------------------------------------------------------------------- #


class Criterion(StrEnum):
    """committed_reference.criterion (contract S2). Routes the gate ladder in Group C:
    artifact_match -> L1+L2+L3 ; checksum -> L1+L2 ; ack_received -> L1 only."""

    ARTIFACT_MATCH = "artifact_match"
    CHECKSUM = "checksum"
    ACK_RECEIVED = "ack_received"


class Attestor(StrEnum):
    """Signing tier flag (contract S9). hash-only ships first; signed adds a
    delivery-attestation check against an allow-list before release (Group D)."""

    HASH_ONLY = "hash-only"
    SIGNED = "signed"


class Verdict(StrEnum):
    """Per-unit outcome (contract S4)."""

    PASS = "pass"
    FAIL = "fail"


# --------------------------------------------------------------------------- #
# Shared validation helpers.
# --------------------------------------------------------------------------- #

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SEQ_KEY = re.compile(r"^(0|[1-9][0-9]*)$")


def _require_sha256_hex(value: str, field: str) -> str:
    """A sha256 hexdigest is exactly 64 lowercase hex chars. Lowercase is required
    (not just conventional): the L2 constant-time compare in Group C matches against
    ``hashlib.sha256(chunk).hexdigest()``, which is always lowercase -- an uppercase
    digest here would silently never settle. Fail fast instead."""
    if not _SHA256_HEX.fullmatch(value):
        raise ValueError(
            f"{field} must be a lowercase 64-char sha256 hex digest, got {value!r}"
        )
    return value


class _Strict(BaseModel):
    """Base for every contract model: reject unknown fields. The v1 surface is frozen,
    so an unexpected key is a caller bug (typo / stale client), not forward-compat."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# IN models.
# --------------------------------------------------------------------------- #


class CommittedReference(_Strict):
    """What the buyer committed to at open (contract S2, decision 1).

    ``expected`` is ALWAYS a ``{seq: sha256}`` map -- a single-artifact demo is a
    1-entry map, not a bare string. Keys are ``str(seq)``; values are lowercase
    sha256 hex digests. For criterion=checksum/ack_received the map may be ``{}``
    (those ladders never reach the L3 identity check).
    """

    criterion: Criterion
    task_id: str
    expected: dict[str, str] = Field(default_factory=dict)

    @field_validator("expected")
    @classmethod
    def _check_expected(cls, v: dict[str, str]) -> dict[str, str]:
        for seq_key, sha in v.items():
            if not _SEQ_KEY.fullmatch(seq_key):
                raise ValueError(
                    "expected keys must be non-negative int strings (str(seq)), "
                    f"got {seq_key!r}"
                )
            _require_sha256_hex(sha, f"expected[{seq_key!r}]")
        return v

    @model_validator(mode="after")
    def _artifact_match_needs_expected(self) -> Self:
        # An artifact_match settlement with no committed hashes would KeyError on the
        # first unit's L3 lookup (expected[str(seq)]). That is a misconfiguration.
        if self.criterion is Criterion.ARTIFACT_MATCH and not self.expected:
            raise ValueError(
                "criterion=artifact_match requires a non-empty expected {seq: sha256} map"
            )
        return self


class SettleOpenIn(_Strict):
    """POST /settle/open request (contract S2)."""

    ref: str
    rate: int = Field(ge=0, description="price released per verified unit")
    max_total: int = Field(ge=0, description="spending cap; remainder stays unspent")
    attestor: Attestor = Attestor.HASH_ONLY
    committed_reference: CommittedReference

    @model_validator(mode="after")
    def _task_id_binds_ref(self) -> Self:
        # Contract S2: committed_reference.task_id == ref. The engine's L3 check calls
        # artifact_match(task_id=ref) directly (contract S5) -- it never reads
        # committed_reference.task_id -- so a divergent task_id would be silently
        # ignored and mislead the caller. Reject it here.
        if self.committed_reference.task_id != self.ref:
            raise ValueError(
                "committed_reference.task_id must equal ref "
                f"({self.committed_reference.task_id!r} != {self.ref!r})"
            )
        return self


class UnitIn(_Strict):
    """One delivered unit (contract S3)."""

    seq: int = Field(ge=0)
    payload_hex: str
    declared_checksum: str | None = None

    @field_validator("payload_hex")
    @classmethod
    def _check_payload_hex(cls, v: str) -> str:
        # Must be decodable exactly as the engine will decode it (bytes.fromhex).
        try:
            bytes.fromhex(v)
        except ValueError as exc:
            raise ValueError(f"payload_hex must be valid hex: {exc}") from exc
        return v

    @field_validator("declared_checksum")
    @classmethod
    def _check_declared_checksum(cls, v: str | None) -> str | None:
        # null => L2 never settles (contract S3). When present it must be a lowercase
        # sha256 hex so the L2 constant-time compare can match sha256(chunk).hexdigest().
        if v is None:
            return None
        return _require_sha256_hex(v, "declared_checksum")


class Attestation(_Strict):
    """Delivery attestation (contract S3/S9). REQUIRED only when the OPEN settlement's
    attestor='signed'; omitted/null in hash-only. That 'required-when-signed' rule is
    enforced by the engine against the open tier (Group D), not by this model alone --
    a single /advance payload does not carry the tier."""

    signer_pubkey: str
    sig: str


class AdvanceIn(_Strict):
    """POST /settle/advance request -- one unit (contract S3)."""

    ref: str
    unit: UnitIn
    attestation: Attestation | None = None


# --------------------------------------------------------------------------- #
# OUT models.
# --------------------------------------------------------------------------- #


class PerUnit(_Strict):
    """Per-unit verdict line (contract S4)."""

    seq: int
    verdict: Verdict
    reason: str


class Receipt(_Strict):
    """The signable settlement core -- the deterministic facts nisha signs over
    (contract S9: an Ed25519 signature 'over receipt+trace'). Distinct from
    ``SettleOut``, which is the full wire response with trace/signature/display.
    ``canonical_bytes()`` yields the exact signable bytes (frozen canonical rule),
    so Group D signs and verifies over a stable encoding."""

    ref: str
    per_unit: list[PerUnit]
    settled_total: int
    remainder_unspent: int

    def canonical_bytes(self) -> bytes:
        """Deterministic signable bytes of this receipt (via the frozen canonical rule)."""
        return canonical_bytes(self.model_dump(mode="json"))


class SettleOut(_Strict):
    """Response shared by /settle/advance and /settle/close (contract S4).

    ``verdict_signature`` is null in the hash-only tier (set by Group D in signed
    tier). ``display`` is the pre-formatted transcript OpenClaw prints verbatim.
    """

    per_unit: list[PerUnit]
    settled_total: int
    remainder_unspent: int
    trace: list[str]
    verdict_signature: str | None = None
    display: str
