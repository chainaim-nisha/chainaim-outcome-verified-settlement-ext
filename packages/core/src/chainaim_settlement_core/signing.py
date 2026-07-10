# SPDX-License-Identifier: Apache-2.0
"""Ed25519 verdict signing for the settlement verifier (Group D, contract S9).

nisha's verifier signs its verdict with its OWN new Ed25519 key -- NOT PR #47 (that is
Service A's emitter key; there is no PR #47 dependency here). The signature covers the
signable settlement core (:class:`~chainaim_settlement_core.contract.Receipt`) together
with the emitted trace, so tampering with either the receipt facts or a trace line
invalidates it.

Signable bytes (FROZEN this Group -- pins what contract S4/S9 leave as "over receipt+trace")::

    verdict_message(receipt, trace) = canonical_bytes({
        "receipt": receipt.model_dump(mode="json"),
        "trace":   list(trace),
    })

produced with the frozen S6 canonical rule (sorted keys, compact separators, UTF-8), so
the signer and any verifier agree on the exact bytes without renegotiation.

Wire encoding (FROZEN): keys and signatures cross the contract boundary as LOWERCASE HEX
of raw bytes -- an Ed25519 public key is 64 hex chars (32 bytes) and a signature is 128
hex chars (64 bytes). This matches the codebase's existing hex convention (sha256
hexdigests, ``UnitIn.payload_hex``), so ``SettleOut.verdict_signature`` and the
``Attestation`` fields are all uniform hex strings.

Ed25519 is the frozen curve for both the verdict signature and delivery attestations; it
is deterministic (a given key signing given bytes yields one fixed signature) and needs no
nonce. Verification is signature verification against a published/allow-listed public key
(contract S9) -- never a key-identity match.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .contract import Receipt, canonical_bytes

__all__ = [
    "SettlementSigner",
    "verdict_message",
    "sign_verdict",
    "verify_verdict",
    "public_key_hex",
    "load_public_key",
]


# --------------------------------------------------------------------------- #
# Frozen signable message (contract S4/S9 -> pinned here).
# --------------------------------------------------------------------------- #


def verdict_message(receipt: Receipt, trace: list[str]) -> bytes:
    """The exact bytes the verdict signature covers: canonical over ``{receipt, trace}``.

    FROZEN this Group. Both the signer and the verifier recompute this identically via the
    frozen S6 canonical rule, so no signature material or field ordering is negotiated on
    the wire -- only the resulting signature hex travels.
    """
    return canonical_bytes(
        {
            "receipt": receipt.model_dump(mode="json"),
            "trace": list(trace),
        }
    )


# --------------------------------------------------------------------------- #
# Key encoding helpers (frozen hex wire form).
# --------------------------------------------------------------------------- #


def public_key_hex(public_key: Ed25519PublicKey) -> str:
    """Lowercase hex of the raw 32-byte Ed25519 public key (the frozen wire form)."""
    return public_key.public_bytes_raw().hex()


def load_public_key(pubkey_hex: str) -> Ed25519PublicKey:
    """Parse a lowercase-hex Ed25519 public key back into a key object.

    Raises ``ValueError`` for non-hex input or a wrong byte length (Ed25519 keys are
    exactly 32 bytes) -- callers treat that as an untrusted/malformed key, never a crash.
    """
    try:
        raw = bytes.fromhex(pubkey_hex.strip())
    except ValueError as exc:
        raise ValueError(f"public key must be hex: {exc}") from exc
    return Ed25519PublicKey.from_public_bytes(raw)  # raises ValueError if not 32 bytes


def _coerce_public_key(public_key: Ed25519PublicKey | str) -> Ed25519PublicKey:
    """Accept either a live key object or its hex form; normalize to a key object."""
    if isinstance(public_key, Ed25519PublicKey):
        return public_key
    return load_public_key(public_key)


# --------------------------------------------------------------------------- #
# The signer -- holds nisha's verdict private key.
# --------------------------------------------------------------------------- #


class SettlementSigner:
    """Holds nisha's Ed25519 verdict private key and signs over the frozen messages.

    The HTTP shell (Group E) constructs ONE signer at startup and reuses it; its public
    key is published at ``GET /pubkey`` so callers can verify ``verdict_signature``.

    Construction (defaults chosen so tests/demo need no key material on disk)::

        signer = SettlementSigner.generate()          # fresh random key
        signer = SettlementSigner.from_seed(seed_32b)  # reproducible from a 32-byte seed

    The private key never leaves this object except as a raw seed via :meth:`seed_hex`
    (for an operator to persist deliberately); it is never logged or serialized otherwise.
    """

    __slots__ = ("_private_key",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key

    @classmethod
    def generate(cls) -> SettlementSigner:
        """A fresh random Ed25519 signer."""
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> SettlementSigner:
        """Reconstruct a signer from its raw 32-byte private seed (reproducible key)."""
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @classmethod
    def from_seed_hex(cls, seed_hex: str) -> SettlementSigner:
        """Reconstruct a signer from a hex-encoded 32-byte private seed."""
        return cls.from_seed(bytes.fromhex(seed_hex.strip()))

    @property
    def public_key(self) -> Ed25519PublicKey:
        """The matching public key object."""
        return self._private_key.public_key()

    @property
    def public_key_hex(self) -> str:
        """Lowercase-hex public key -- what goes on the wire / into an allow-list."""
        return public_key_hex(self.public_key)

    def seed_hex(self) -> str:
        """Hex of the raw 32-byte private seed. Handle as a secret; for deliberate
        persistence only (e.g. an operator pinning a stable verifier key)."""
        return self._private_key.private_bytes_raw().hex()

    def sign_bytes(self, message: bytes) -> str:
        """Sign arbitrary bytes; return the signature as lowercase hex (128 chars)."""
        return self._private_key.sign(message).hex()

    def sign(self, receipt: Receipt, trace: list[str]) -> str:
        """Sign a settlement verdict (receipt + trace); return the signature hex.

        This is the value that fills ``SettleOut.verdict_signature`` in the signed tier.
        """
        return self.sign_bytes(verdict_message(receipt, trace))


# --------------------------------------------------------------------------- #
# Module-level sign / verify (stateless verify for callers holding only a pubkey).
# --------------------------------------------------------------------------- #


def sign_verdict(
    receipt: Receipt, trace: list[str], *, signer: SettlementSigner
) -> str:
    """Sign ``receipt + trace`` with ``signer``; return the verdict signature hex."""
    return signer.sign(receipt, trace)


def verify_verdict(
    receipt: Receipt,
    trace: list[str],
    signature_hex: str,
    *,
    public_key: Ed25519PublicKey | str,
) -> bool:
    """Verify a verdict signature over ``receipt + trace`` against ``public_key``.

    ``public_key`` may be a live key object or its hex form. Returns ``False`` (never
    raises) for a bad/short/non-hex signature, a malformed public key, or any mismatch --
    so a tampered receipt, an altered trace line, or the wrong key all read as invalid.
    """
    try:
        key = _coerce_public_key(public_key)
        signature = bytes.fromhex(signature_hex.strip())
    except ValueError:
        return False
    try:
        key.verify(signature, verdict_message(receipt, trace))
    except (InvalidSignature, ValueError):
        # InvalidSignature on mismatch; ValueError guards builds that reject a
        # wrong-length signature with ValueError instead -- both mean "not valid".
        return False
    return True
