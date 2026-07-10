"""chainaim-settlement-core: generic outcome-verified settlement engine (nisha).

Imports the FROZEN PR #61 engine (chainaim-nisha/nandatown, import-only, never edited):
  - OutcomeVerifiedSettlement  (payment ledger plugin: open/advance/close a metered stream)
  - UnitContext, artifact_match (gate seam; L3 identity check, called directly like the b7 tests)

and adds, in this package: the L1/L2/L3 gate runner (gates.py), the per-tick release loop
(engine.py), the trace emitter (trace.py), the attestor tiers (attestor.py), and Ed25519
verdict signing (signing.py). No business vocabulary lives here.

Pinned to frozen PR #61 commit 20e18b80f5f44c35cbc1494a83151e375112773e (see workspace root).
"""

__version__ = "0.1.0"
