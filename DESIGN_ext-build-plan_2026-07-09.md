# Phase-2 Combo - `-ext` Build Plan (execution groups)

**Repo:** `chainaim-outcome-verified-settlement-ext` (nisha, greenfield)
**Path:** `C:\SATHYA\CHAINAIM3003\mcp-servers\FINAGENTS\FINAGENTS1\chainaim-outcome-verified-settlement-ext`
**Date:** 2026-07-09
**Builds against:** `ph2-combo-1_verifier-contract-v1_2026-07-09.md` (FROZEN v1.0).
**Total:** ~26 files (18 source + 8 tests), 0 changed in existing code, PR #61 import-only.

Principle: each Group is a coherent milestone that leaves the repo GREEN (its tests pass) before the next Group starts. Build module + its test together. I write; you run (no exec tool on your Windows box - Rule 9). Each Group ends with ONE verify command you run and report.

Dependency order:  A -> B -> {C, D} -> E -> F   (C and D are independent of each other; both need A+B.)

Flags exposed (Rule 8, defaults in parens): `--attestor` hash-only|signed (hash-only) - `--algo` checksum digest (sha256) - `/demo/settlement?case=` good|bad (good) - `--host`/`--port` on the service.

---

## GROUP A - Skeleton & fork wiring (make it installable; PR #61 imports)
**Why first:** nothing can import or run until the package resolves and the PR #61 fork is importable. This is the only Group with a manual input (the SHA).

**Files (6):**
- `README.md`, `LICENSE` (Apache-2.0 - grounded: gates.py SPDX header), `.gitignore`  (repo root)
- `packages/core/pyproject.toml`  ([tool.uv.sources] PR #61 fork, `rev=<SHA>`; `cryptography>=42` explicit; `requires-python>=3.12`)
- `packages/core/src/chainaim_settlement_core/__init__.py`
- `services/settlement-verifier/pyproject.toml`  (core editable + PR #61 fork SHA)

**AI can do:** write all 6; propose the SHA by reading the local fork ref at `...\chainaim-nisha\nandatown`.
**Manual - you:** confirm that proposed SHA is the frozen commit you want pinned (I walk this as one step).
**Verify (you run):**
```
cd packages/core && uv sync
uv run python -c "from nest_plugins_reference.payments.outcome_verified_settlement import OutcomeVerifiedSettlement; from nest_core.scenarios_builtin.gates import UnitContext, artifact_match, json_schema, reference_match; print('imports ok')"
```
Green = "imports ok" prints. This alone proves the one-fork wiring works.

---

## GROUP B - Contract + canonical (the data model)
**Why:** every module below references these shapes; the canonical rule (contract S6) must exist before any hash is computed.

**Files (2):**
- `packages/core/src/chainaim_settlement_core/contract.py`  (pydantic: `SettleOpenIn`, `CommittedReference{criterion, task_id, expected:{seq:sha256}}`, `UnitIn`, `AdvanceIn`, `Attestation`, `PerUnit`, `SettleOut/Receipt`, `Verdict`; + `canonical_bytes(obj)->bytes` = `json.dumps(obj, sort_keys=True, separators=(",",":")).encode("utf-8")` and `committed_hash(obj)->hexdigest`)
- `packages/core/tests/test_contract.py`  (pydantic IN/OUT validation; `expected` is a `{seq:sha256}` map incl. 1-entry degenerate; canonical-bytes determinism/stability)

**Verify:** `uv run pytest packages/core/tests/test_contract.py -v`

---

## GROUP C - Settlement core (gates + engine + trace) -- the heart
**Why:** the actual L1->L2->L3 decision, per-tick release, and trace. Needs A (PR #61 import) + B (types).

**Files (6):**
- `gates.py`  (MINIMAL PR #61 surface = import ONLY `UnitContext` + `artifact_match` from `nest_core.scenarios_builtin.gates`. Build `UnitContext(ref,seq,ack_received,chunk,declared_checksum)`. L1 ack = bool; L2 checksum = stdlib `hmac.compare_digest(hashlib.sha256(chunk).hexdigest(), declared_checksum)` (null checksum never settles); L3 = `artifact_match(ctx, expected_sha256=expected[seq], task_id=ref)` called DIRECTLY like b7. NO gate classes, NO `Gate.from_name`. Verified: these imports pull pydantic + stdlib only - no nest-shell/LLM/driver.)
- `engine.py`  (import `OutcomeVerifiedSettlement`; per-tick release loop; invariant `billed <= rate * verified`; remainder unspent; deterministic close)
- `trace.py`  (emit the contract S7 grammar: stream-open/tick/ack/gate/stream-close)
- tests: `test_gates.py` (artifact_match match/mismatch, L2 checksum, L1 ack, null-checksum-never-settles, swapped-honest-checksum fails L3), `test_engine.py` (invariant, remainder, deterministic close), `test_trace.py` (grammar well-formed)

**Verify:** `uv run pytest packages/core/tests/test_gates.py packages/core/tests/test_engine.py packages/core/tests/test_trace.py -v`

---

## GROUP D - Crypto & trust (signing + attestor tiers)  [independent of C]
**Why:** verdict signing + the hash-only|signed tier gate. Needs only A (cryptography). Can be built in parallel with C.

**Files (4):**
- `signing.py`  (Ed25519 keygen; sign/verify receipt+trace; tamper detection)
- `attestor.py`  (`--attestor` hash-only|signed; in `signed`, verify delivery attestation sig against a trusted-pubkey allow-list BEFORE release; signature VERIFICATION vs published key, not key-match)
- tests: `test_signing.py` (sign/verify + tamper), `test_attestor.py` (hash-only skips sig; signed requires+verifies; allow-list reject)

**Verify:** `uv run pytest packages/core/tests/test_signing.py packages/core/tests/test_attestor.py -v`

---

## GROUP E - Service shell (FastAPI app + SKILL.md)
**Why:** the HTTP surface OpenClaw/judges hit. Needs B+C+D (imports all core).

**Files (4):**
- `services/settlement-verifier/app.py`  (house trio `/health` `/skill.md` `/pubkey`; `/demo/settlement`; `/settle/open` `/advance` `/close`; `/verify`; returns `display` transcript verbatim)
- `services/settlement-verifier/SKILL.md`  (generic verifier skill; "print the `display` field verbatim")
- tests: `test_app.py` (FastAPI TestClient: house trio; open/advance/close happy path; wrong-unit refused; `/demo/settlement` match vs mismatch), `conftest.py` (fixtures)

**Verify:** `uv run pytest services/settlement-verifier/tests/ -v`

---

## GROUP F - Self-contained demo + smoke (the guaranteed deliverable)
**Why:** nisha's >=1 submittable = this service demoed self-contained, running WITHOUT sathya. Money shot: good settles per-tick; bad fails, closes, remainder unspent.

**Files (4):**
- `services/settlement-verifier/demo_data/committed_reference.json`  (committed `{seq:sha256}`, hashes computed via `canonical_bytes`)
- `.../demo_data/delivery_good.json`  (matching bytes -> L2+L3 pass -> settle)
- `.../demo_data/delivery_bad.json`  (one corrupted -> L2 fail; one swapped-honest-checksum -> L3 fail)
- `services/settlement-verifier/smoke.py`  (`python smoke.py [good|bad]` - runs the demo in-process, prints transcript; no network)

**Verify (you run):**
```
uv run python services/settlement-verifier/smoke.py good      # settles per-tick
uv run python services/settlement-verifier/smoke.py bad       # fails, closes, remainder unspent
uv run pytest -v                                              # ALL green
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

---

## Manual steps (walked ONE at a time - Rule 9), all AFTER the code is green
Not part of the build; the deploy phase.
1. Get/confirm the PR #61 fork commit SHA to pin (Group A - I propose from the local clone; you confirm).
2. Create the GitHub repo + push (I prep tree + commit messages).
3. Confirm the fork is public or add a deploy token (Render/Railway build).
4. Host + keep-warm (cold start kills judge probes).
5. Paste the hosted URL into `SKILL.md ## Base URL` + OpenClaw.

## Carried items - NOT blockers for `-ext`
- PR #47 emitter signing = Service A / sathya side; `-ext`'s signing is nisha's OWN new Ed25519 verdict key -> no PR #47 dependency here.
- Root LICENSE Apache-2.0: grounded via gates.py SPDX header; safe to write in Group A.

## Execution rhythm
I write a Group's files -> you run its ONE verify command -> report -> I fix if red -> next Group. Nothing hand-waved; each Group green before the next.
