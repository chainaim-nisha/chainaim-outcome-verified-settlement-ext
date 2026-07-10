# chainaim-outcome-verified-settlement-ext

Generic **outcome-verified settlement verifier** (nisha side of the Phase-2 combo).

Given a delivered artifact, a committed reference (a `{seq: sha256}` map), a rate, and a cap,
it opens a metered stream, runs each unit through **L1 (delivered) -> L2 (intact) -> L3
(matches the committed hash)**, releases `rate` per passing unit while `billed <= rate x verified`,
closes deterministically, emits a trace, and signs its verdict. It has **no business
vocabulary** -- no field/op/value, no schemas, no quantity/grade/date logic. All business
judgment happens upstream (sathya) and is frozen into the committed hash; this service proves
identity / integrity / delivery only.

## Architecture

- `packages/core/` -- the reusable engine. Imports the FROZEN PR #61 fork
  (`chainaim-nisha/nandatown`, import-only, pinned to commit
  `20e18b80f5f44c35cbc1494a83151e375112773e`): `OutcomeVerifiedSettlement` (payment ledger)
  and `UnitContext` + `artifact_match` (gate seam, called directly). Adds the L1/L2/L3 gate
  runner, per-tick release loop, trace, attestor tiers, and Ed25519 verdict signing.
- `services/settlement-verifier/` -- the FastAPI HTTP shell. House trio (`/health`,
  `/skill.md`, `/pubkey`) + `/demo/settlement` + `/settle/open|advance|close` + `/verify`.

The two repos in the combo share **no code** -- they meet only over HTTP + a cert JSON.
This repo imports the PR #61 fork; the sathya negotiation repo imports the PR #47 fork.

## Install (local dev)

Requires Python >= 3.12 and [uv](https://docs.astral.sh/uv/). The PR #61 fork must sit next to
this repo under `FINAGENTS1/chainaim-nisha/nandatown`.

```
cd packages/core
uv sync
uv run python -c "from nest_plugins_reference.payments.outcome_verified_settlement import OutcomeVerifiedSettlement; from nest_core.scenarios_builtin.gates import UnitContext, artifact_match; print('imports ok')"
```

For deploy, switch the workspace `[tool.uv.sources]` from the LOCAL editable paths to the
GITHUB `rev`-pinned blocks (see comments in the root `pyproject.toml`).

## License

Apache-2.0 (imports the Apache-2.0 PR #61 engine).
