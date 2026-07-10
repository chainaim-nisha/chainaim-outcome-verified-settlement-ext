"""Group E fixtures for the settlement-verifier HTTP tests.

The service is a flat module (``services/settlement-verifier/app.py``, package = false, run
via ``uvicorn app:app`` like Service A), so it is not importable as an installed package.
This conftest puts the service directory on ``sys.path`` so ``import app`` resolves, then
exposes a TestClient wired to an injected config (a known verdict signer + a known
trusted-pubkey allow-list) so the signed-tier paths are testable deterministically.
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

from app import VerifierConfig, create_app  # noqa: E402
from chainaim_settlement_core.attestor import TrustedKeys  # noqa: E402
from chainaim_settlement_core.signing import SettlementSigner  # noqa: E402


@pytest.fixture
def builder() -> SettlementSigner:
    """The seller/builder key that signs delivery attestations (trusted by ``config``)."""
    return SettlementSigner.generate()


@pytest.fixture
def verifier_signer() -> SettlementSigner:
    """The verifier's own verdict-signing key (published at /pubkey)."""
    return SettlementSigner.generate()


@pytest.fixture
def config(
    builder: SettlementSigner, verifier_signer: SettlementSigner
) -> VerifierConfig:
    """Config trusting exactly the ``builder`` key for signed-tier attestations."""
    return VerifierConfig(
        signer=verifier_signer,
        trusted_keys=TrustedKeys([builder.public_key_hex]),
    )


@pytest.fixture
def client(config: VerifierConfig) -> Iterator[TestClient]:
    # Context-manager form: one persistent event loop for ALL requests in a test, so the
    # async engine's per-ref session (open -> advance -> close spanning requests) is driven
    # on a single loop rather than a fresh loop per request.
    with TestClient(create_app(config)) as test_client:
        yield test_client
