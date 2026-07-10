# SPDX-License-Identifier: Apache-2.0
"""FastAPI HTTP shell for the generic settlement verifier (Group E).

The thin HTTP surface OpenClaw / judges hit. It owns NO business logic and NO gate logic:
it validates the frozen contract shapes, drives the async :class:`SettlementEngine`
(one session per open ``ref``), applies the attestor tier guard before release in the
``signed`` tier, and returns the pre-formatted ``display`` transcript verbatim.

Endpoints (contract S1, mirroring Service A's house shape):
    GET  /health            liveness
    GET  /skill.md          serves this service's SKILL.md verbatim (text/markdown)
    GET  /pubkey            the verifier's Ed25519 verdict-signing public key (hex)
    GET  /demo/settlement   self-contained match-vs-mismatch demo (?case=good|bad)
    POST /settle/open       open a metered stream bound to ref
    POST /settle/advance    submit one unit -> gate ladder -> settle or refuse/close
    POST /settle/close      close the stream -> final receipt + trace (+ verdict sig)
    POST /verify            offline re-check of a returned receipt/trace verdict signature

Signed tier (contract S9): /settle/advance runs :func:`clear_for_release` BEFORE the engine
advances; a missing / untrusted / invalid delivery attestation is refused with HTTP 403 and
the stream is left open (a trust refusal is distinct from a content-gate failure, which the
engine handles by closing). On a cleared signed-tier advance/close the verifier fills
``verdict_signature`` by signing ``receipt + trace`` with its own key. hash-only leaves it null.

Configuration (Rule 8 -- explicit, flag/env driven):
    SETTLEMENT_SIGNING_SEED     hex 32-byte Ed25519 seed for a stable verdict key
                                (default: a fresh random key per process)
    SETTLEMENT_TRUSTED_PUBKEYS  comma-separated hex pubkeys for the signed-tier allow-list
    SETTLEMENT_PAYEE_PUBKEY     optional payee pubkey to exclude (enforces S9's NON-PAYEE key)

Session state is in-process (one worker). For multi-worker hosting a shared store would be
needed; the self-contained demo and single-instance judging path do not require it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from chainaim_settlement_core.attestor import TrustedKeys, clear_for_release
from chainaim_settlement_core.contract import (
    AdvanceIn,
    Attestor,
    CommittedReference,
    Criterion,
    Receipt,
    SettleOpenIn,
    SettleOut,
    UnitIn,
    canonical_bytes,
)
from chainaim_settlement_core.engine import SettlementEngine
from chainaim_settlement_core.signing import SettlementSigner, verify_verdict

SKILL_PATH = Path(__file__).with_name("SKILL.md")

DEMO_REF = hashlib.sha256(b"demo-settlement").hexdigest()
DEMO_RATE = 100
DEMO_MAX_TOTAL = 500


# --------------------------------------------------------------------------- #
# Request / response models that are local to the service (contract OUT reused).
# --------------------------------------------------------------------------- #


class CloseIn(BaseModel):
    """POST /settle/close body (contract S1: ``{ref}``)."""

    ref: str


class VerifyIn(BaseModel):
    """POST /verify body -- a returned receipt/trace plus the verdict signature to re-check."""

    receipt: Receipt
    trace: list[str]
    verdict_signature: str | None = None


class VerifyOut(BaseModel):
    """POST /verify result. ``reason`` is a stable token, not free text."""

    valid: bool
    reason: str


class PubkeyOut(BaseModel):
    """GET /pubkey result: the verifier's Ed25519 verdict key, lowercase hex."""

    pubkey: str
    algo: str = "ed25519"


# --------------------------------------------------------------------------- #
# Service configuration + per-ref session.
# --------------------------------------------------------------------------- #


@dataclass
class VerifierConfig:
    """Everything the app needs that a test or an operator may want to inject."""

    signer: SettlementSigner
    trusted_keys: TrustedKeys
    payee_pubkey: str | None = None

    @classmethod
    def from_env(cls) -> VerifierConfig:
        """Build config from environment (see module docstring for the variables)."""
        seed = os.environ.get("SETTLEMENT_SIGNING_SEED")
        signer = (
            SettlementSigner.from_seed_hex(seed)
            if seed
            else SettlementSigner.generate()
        )
        raw = os.environ.get("SETTLEMENT_TRUSTED_PUBKEYS", "")
        trusted = TrustedKeys(part.strip() for part in raw.split(",") if part.strip())
        payee = os.environ.get("SETTLEMENT_PAYEE_PUBKEY") or None
        return cls(signer=signer, trusted_keys=trusted, payee_pubkey=payee)


@dataclass
class _Session:
    """One open settlement: its engine, its tier, and a lock serializing its mutations."""

    engine: SettlementEngine
    tier: Attestor
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# --------------------------------------------------------------------------- #
# Self-contained demo (contract/architecture S4 of the design) -- hash-only.
# --------------------------------------------------------------------------- #


def _demo_artifact(seq: int, body: str) -> tuple[str, str]:
    """(payload_hex, sha256_hex) for a conforming demo unit embedding task_id=DEMO_REF."""
    chunk = canonical_bytes({"seq": seq, "task_id": DEMO_REF, "body": body})
    return chunk.hex(), hashlib.sha256(chunk).hexdigest()


async def _run_demo(case: Literal["good", "bad"]) -> SettleOut:
    """Open a stream and advance a matching unit, then (good) another match or
    (bad) a swapped honest-checksum unit that fails L3, closing with remainder unspent."""
    ph0, sha0 = _demo_artifact(0, "invoice-conforming-0")
    ph1, sha1 = _demo_artifact(1, "invoice-conforming-1")
    engine = SettlementEngine()
    await engine.open(
        SettleOpenIn(
            ref=DEMO_REF,
            rate=DEMO_RATE,
            max_total=DEMO_MAX_TOTAL,
            committed_reference=CommittedReference(
                criterion=Criterion.ARTIFACT_MATCH,
                task_id=DEMO_REF,
                expected={"0": sha0, "1": sha1},
            ),
        )
    )
    await engine.advance(
        AdvanceIn(
            ref=DEMO_REF, unit=UnitIn(seq=0, payload_hex=ph0, declared_checksum=sha0)
        )
    )
    if case == "good":
        await engine.advance(
            AdvanceIn(
                ref=DEMO_REF,
                unit=UnitIn(seq=1, payload_hex=ph1, declared_checksum=sha1),
            )
        )
    else:
        # A DIFFERENT valid artifact, honestly checksummed -> passes L2, fails L3 identity.
        ph_wrong, sha_wrong = _demo_artifact(99, "a-different-artifact")
        await engine.advance(
            AdvanceIn(
                ref=DEMO_REF,
                unit=UnitIn(seq=1, payload_hex=ph_wrong, declared_checksum=sha_wrong),
            )
        )
    return await engine.close(DEMO_REF)


# --------------------------------------------------------------------------- #
# App factory.
# --------------------------------------------------------------------------- #


def create_app(config: VerifierConfig | None = None) -> FastAPI:
    """Build the FastAPI app. Pass a :class:`VerifierConfig` to inject a signer / allow-list
    (tests do this); with no argument the config is read from the environment."""
    cfg = config or VerifierConfig.from_env()
    app = FastAPI(title="chainaim settlement-verifier", version="0.1.0")

    sessions: dict[str, _Session] = {}
    open_lock = asyncio.Lock()

    def _sign_if_signed(out: SettleOut, sess: _Session) -> SettleOut:
        """In the signed tier, fill verdict_signature over the current receipt + trace."""
        if sess.tier is Attestor.SIGNED:
            sig = cfg.signer.sign(sess.engine.receipt(), out.trace)
            return out.model_copy(update={"verdict_signature": sig})
        return out

    @app.get("/")
    async def index() -> dict[str, object]:
        """Service index -- a discovery entry point for humans and agents.

        The endpoint map is built by introspecting the live route table, so it can
        never drift from what is actually mounted. The authoritative capability
        document is GET /skill.md; interactive API docs are at GET /docs.
        """
        endpoints = dict(
            sorted(
                (
                    route.path,
                    sorted(m for m in route.methods if m not in {"HEAD", "OPTIONS"}),
                )
                for route in app.routes
                if isinstance(route, APIRoute) and route.path != "/"
            )
        )
        return {
            "service": "settlement-verifier",
            "version": app.version,
            "status": "ok",
            "skill_md": "/skill.md",
            "docs": "/docs",
            "endpoints": endpoints,
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "settlement-verifier"}

    @app.get("/skill.md", response_class=PlainTextResponse)
    async def skill_md() -> str:
        try:
            return SKILL_PATH.read_text(encoding="utf-8")
        except OSError:
            return "# settlement-verifier\n\nSKILL.md is unavailable.\n"

    @app.get("/pubkey", response_model=PubkeyOut)
    async def pubkey() -> PubkeyOut:
        return PubkeyOut(pubkey=cfg.signer.public_key_hex)

    @app.get("/demo/settlement", response_model=SettleOut)
    async def demo_settlement(case: Literal["good", "bad"] = "good") -> SettleOut:
        return await _run_demo(case)

    @app.post("/settle/open", response_model=SettleOut)
    async def settle_open(body: SettleOpenIn) -> SettleOut:
        async with open_lock:
            if body.ref in sessions:
                raise HTTPException(
                    status_code=409,
                    detail=f"settlement already open for ref {body.ref!r}",
                )
            engine = SettlementEngine()
            try:
                out = await engine.open(body)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            sessions[body.ref] = _Session(engine=engine, tier=body.attestor)
            return out

    @app.post("/settle/advance", response_model=SettleOut)
    async def settle_advance(body: AdvanceIn) -> SettleOut:
        sess = sessions.get(body.ref)
        if sess is None:
            raise HTTPException(
                status_code=404, detail=f"no open settlement for ref {body.ref!r}"
            )
        async with sess.lock:
            if sess.tier is Attestor.SIGNED:
                decision = clear_for_release(
                    attestor=sess.tier,
                    ref=body.ref,
                    unit=body.unit,
                    attestation=body.attestation,
                    trusted_keys=cfg.trusted_keys,
                    payee_pubkey=cfg.payee_pubkey,
                )
                if not decision.cleared:
                    # Trust refusal: unit never enters the meter; stream stays open.
                    raise HTTPException(status_code=403, detail=decision.reason)
            try:
                out = await sess.engine.advance(body)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return _sign_if_signed(out, sess)

    @app.post("/settle/close", response_model=SettleOut)
    async def settle_close(body: CloseIn) -> SettleOut:
        sess = sessions.get(body.ref)
        if sess is None:
            raise HTTPException(
                status_code=404, detail=f"no open settlement for ref {body.ref!r}"
            )
        async with sess.lock:
            out = await sess.engine.close(body.ref)
            return _sign_if_signed(out, sess)

    @app.post("/verify", response_model=VerifyOut)
    async def verify(body: VerifyIn) -> VerifyOut:
        # Offline re-check of the verifier's OWN verdict signature over receipt + trace.
        if not body.verdict_signature:
            return VerifyOut(valid=False, reason="no-signature")
        ok = verify_verdict(
            body.receipt,
            body.trace,
            body.verdict_signature,
            public_key=cfg.signer.public_key,
        )
        return VerifyOut(
            valid=ok, reason="signature-valid" if ok else "signature-invalid"
        )

    return app


# Module-level app for `uvicorn app:app` (config from environment).
app = create_app()
