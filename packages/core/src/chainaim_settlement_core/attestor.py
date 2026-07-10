# SPDX-License-Identifier: Apache-2.0
"""Attestor tier guard (Group D, contract S9): ``hash-only`` | ``signed``.

Decides whether ONE delivered unit is cleared for release BEFORE the engine advances. The
engine (:mod:`.engine`) is deliberately tier-agnostic and never verifies a delivery
attestation itself; the HTTP shell (Group E) calls :func:`clear_for_release` first and, in
the signed tier, only calls ``engine.advance()`` on a cleared result. This keeps the
release decision and the trust decision separable and each independently testable.

Tiers (one ``--attestor`` flag at the service; ``attestor`` arg here):

* **hash-only** (ships first): no delivery attestation is required and this guard always
  clears. Identity and integrity still come from the in-engine L1/L2/L3 gates; the tier
  simply adds no signature step.
* **signed**: the delivered unit MUST carry a delivery :class:`~chainaim_settlement_core.contract.Attestation`
  whose ``signer_pubkey`` is on the verifier's trusted-pubkey allow-list AND whose ``sig``
  verifies over the frozen attestation message. This is signature VERIFICATION against a
  published/allow-listed key (contract S9) -- NOT a key-identity match. When the payee's
  own pubkey is known it can be excluded via ``payee_pubkey`` so the co-signer is a
  NON-PAYEE key (contract S9).

FROZEN delivery-attestation message (pins what S9 leaves open)::

    attestation_message(ref, unit) = canonical_bytes({
        "ref":  ref,
        "unit": unit.model_dump(mode="json"),   # {seq, payload_hex, declared_checksum}
    })

binding each attestation to the deal ``ref`` and the EXACT delivered bytes: a signature
made for one unit will not verify if the payload is swapped, so the signed tier catches a
substituted delivery even before the content gates run.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature

from .contract import Attestation, Attestor, UnitIn, canonical_bytes
from .signing import SettlementSigner, load_public_key

__all__ = [
    "TrustedKeys",
    "AttestorDecision",
    "attestation_message",
    "clear_for_release",
    "sign_attestation",
]


def _normalize(pubkey_hex: str) -> str:
    """Canonical allow-list / comparison form for a hex pubkey: stripped + lowercased."""
    return pubkey_hex.strip().lower()


# --------------------------------------------------------------------------- #
# Frozen delivery-attestation message (contract S9 -> pinned here).
# --------------------------------------------------------------------------- #


def attestation_message(ref: str, unit: UnitIn) -> bytes:
    """The exact bytes a delivery attestation covers: canonical over ``{ref, unit}``.

    FROZEN this Group. The builder/seller (Service B, or the self-contained demo) signs
    these bytes; the verifier recomputes them identically, so the signature binds the
    attestation to this deal and this unit's delivered payload.
    """
    return canonical_bytes({"ref": ref, "unit": unit.model_dump(mode="json")})


# --------------------------------------------------------------------------- #
# The trusted-pubkey allow-list (frozen representation).
# --------------------------------------------------------------------------- #


class TrustedKeys:
    """The verifier's allow-list of trusted delivery-attestation pubkeys.

    Frozen representation: an immutable set of lowercase-hex Ed25519 public keys.
    Membership is case-insensitive on hex. Constructed once from the verifier's config
    (Group E) and passed into :func:`clear_for_release`.

        ring = TrustedKeys(["a1b2...", "c3d4..."])
        signer_pubkey in ring    # -> bool
    """

    __slots__ = ("_keys",)

    def __init__(self, pubkeys_hex: Iterable[str] = ()) -> None:
        self._keys = frozenset(_normalize(pk) for pk in pubkeys_hex)

    def __contains__(self, pubkey_hex: str) -> bool:
        return _normalize(pubkey_hex) in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    def __iter__(self):
        return iter(self._keys)

    def with_key(self, pubkey_hex: str) -> TrustedKeys:
        """Return a NEW allow-list that also trusts ``pubkey_hex`` (immutable update)."""
        return TrustedKeys([*self._keys, pubkey_hex])


# --------------------------------------------------------------------------- #
# The decision.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AttestorDecision:
    """Outcome of the tier guard for one unit.

    ``reason`` is a stable machine token (never free text), so it can drive a trace line or
    an HTTP error verbatim. Known tokens:
    ``hash-only``, ``attested``, ``attestation-required``, ``untrusted-pubkey``,
    ``payee-signed``, ``bad-pubkey``, ``bad-attestation-sig``.
    """

    cleared: bool
    reason: str


def clear_for_release(
    *,
    attestor: Attestor,
    ref: str,
    unit: UnitIn,
    attestation: Attestation | None,
    trusted_keys: TrustedKeys | None = None,
    payee_pubkey: str | None = None,
) -> AttestorDecision:
    """Decide whether ``unit`` may proceed to engine release under ``attestor``.

    In ``hash-only`` the guard always clears (no signature step). In ``signed`` the unit's
    delivery attestation must be present, signed by an allow-listed (and, if
    ``payee_pubkey`` is given, non-payee) key, and verify over :func:`attestation_message`.

    Args (Rule 8 -- behavior is configured by explicit arguments):
        attestor:     the tier from the open settlement (``--attestor``).
        ref, unit:    the deal ref and the delivered unit being cleared.
        attestation:  the delivery attestation carried on the /advance payload (or None).
        trusted_keys: the verifier's allow-list; treated as EMPTY when None (nothing
                      clears in the signed tier -- fail closed, never open).
        payee_pubkey: optional known payee pubkey to exclude (enforces S9's NON-PAYEE key).
    """
    # hash-only: no delivery-attestation gate at all.
    if attestor is Attestor.HASH_ONLY:
        return AttestorDecision(True, "hash-only")

    # signed tier from here -- fail closed on anything missing or unverifiable.
    ring = trusted_keys if trusted_keys is not None else TrustedKeys()

    if attestation is None:
        return AttestorDecision(False, "attestation-required")

    signer = _normalize(attestation.signer_pubkey)

    # Allow-list membership: the key must be published/trusted in advance.
    if signer not in ring:
        return AttestorDecision(False, "untrusted-pubkey")

    # NON-PAYEE enforcement (only when the payee key is known to the verifier).
    if payee_pubkey is not None and signer == _normalize(payee_pubkey):
        return AttestorDecision(False, "payee-signed")

    # Signature verification over the frozen attestation message -- NOT a key match.
    try:
        public_key = load_public_key(signer)
    except ValueError:
        return AttestorDecision(False, "bad-pubkey")
    try:
        signature = bytes.fromhex(attestation.sig.strip())
    except ValueError:
        return AttestorDecision(False, "bad-attestation-sig")
    try:
        public_key.verify(signature, attestation_message(ref, unit))
    except (InvalidSignature, ValueError):
        # InvalidSignature on mismatch; ValueError guards builds that reject a
        # wrong-length signature with ValueError instead -- both fail closed.
        return AttestorDecision(False, "bad-attestation-sig")

    return AttestorDecision(True, "attested")


# --------------------------------------------------------------------------- #
# Builder-side helper (the seller / the self-contained demo produce attestations).
# --------------------------------------------------------------------------- #


def sign_attestation(
    ref: str, unit: UnitIn, *, signer: SettlementSigner
) -> Attestation:
    """Produce a delivery :class:`Attestation` over ``(ref, unit)`` with ``signer``.

    This is the builder/seller action (Service B), reused by the self-contained demo
    (Group F) to fabricate a valid signed delivery without sathya. ``signer_pubkey`` and
    ``sig`` are lowercase hex (the frozen wire encoding).
    """
    return Attestation(
        signer_pubkey=signer.public_key_hex,
        sig=signer.sign_bytes(attestation_message(ref, unit)),
    )
