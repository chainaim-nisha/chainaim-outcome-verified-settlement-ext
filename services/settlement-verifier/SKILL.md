---
name: outcome-verified-settlement-verifier
description: Releases streaming payment only for delivered units that provably match a hash committed up front (identity + integrity + delivery), and returns a signed, offline-verifiable receipt. Use when one agent must pay another for delivered work, data, or artifacts and wants to pay only for items that exactly match what was agreed.
version: "1.0"
author: ChainAIM
github: chainaim-nisha
metadata:
  openclaw:
    requires:
      bins: [curl]
---

# Outcome-Verified Settlement Verifier

**Pay for delivery — but only for work that provably matches what was agreed, and refuse to pay for anything that doesn't.**

An agent releases money one unit at a time as each delivered item is verified against a hash committed up front. A matching item is paid; a missing, corrupted, or swapped item is refused and the stream closes with the rest of the budget unspent. It checks **identity + integrity + delivery only** — all business judgement (grade, quantity, date, schema) is decided upstream and frozen into the committed hash before it ever reaches this service.

**Use this when** one agent owes another for delivered work, data, or artifacts and wants a guarantee it pays *only* for items that exactly match what was committed — plus a signed, independently re-checkable receipt of who was paid and why. Pairs naturally with a negotiation/certifier agent upstream (which decides the terms and produces the committed hashes) and an escrow or marketplace agent downstream (which acts on the signed verdict).

**Verified end-to-end:** given only this file's URL, a stock OpenClaw agent fetched the document, made the single `GET /demo/settlement` call, and printed the settlement transcript — no human steps.

**Tags:** settlement · payments · streaming · verification · trust · integrity · ed25519 · receipts · agents

## Base URL

https://chainaim-outcome-verified-settlement-ext-production.up.railway.app

## Quick start — one call, no setup

No keys, no input, no human step:

```
curl "https://chainaim-outcome-verified-settlement-ext-production.up.railway.app/demo/settlement"
```

The `display` field comes back as a plain-English transcript anyone can read:

```
Settlement complete: deal 174b5d7a…
Rule: pay 100 per delivered item, only if it exactly matches what was promised.
  Item 0: PAID - genuine, matched what was promised.
  Item 1: PAID - genuine, matched what was promised.
Total paid: 200 | Budget unspent: 300 | Status: FINISHED
```

Add `?case=bad` to see the failure path — one item is a *different* artifact carrying an honest checksum, so it passes the integrity check but fails the identity check; payment stops and the remainder stays unspent:

```
  Item 0: PAID - genuine, matched what was promised.
  Item 1: NOT PAID - did not match what was promised; payment stopped here.
Total paid: 100 | Budget unspent: 400 | Status: FINISHED
```

## How the agent should use this

1. Call `GET /demo/settlement` (add `?case=bad` for the failure path).
2. Print the `display` field **verbatim** — it is already a complete, plain-English transcript of the whole settlement (which items were paid, which were refused and why, the totals, and the final status). Do not summarise or reformat it.

To settle a real deal instead of the demo: `POST /settle/open` (commit the expected hashes) → `POST /settle/advance` once per delivered unit → `POST /settle/close`. Exact request/response shapes are below.

## Judge test (runnable from this file alone, no setup)

Any agent or human can validate this skill with two calls:

1. `GET /demo/settlement` — the good path. Expect both items `"verdict":"pass"`, `settled_total` **200**, and a `display` ending `Total paid: 200 | Budget unspent: 300 | Status: FINISHED`.
2. `GET /demo/settlement?case=bad` — the failure path. Expect item 1 `"verdict":"fail"` with `"reason":"artifact-mismatch"`, `remainder_unspent` **400**, and a `display` line reading `Item 1: NOT PAID - ...; payment stopped here.`

Passing both proves the core property in under ten seconds: it pays for what matches and refuses what doesn't.

## Endpoints

The demo and the settle endpoints all return the **same object** (`SettleOut`):

| field | meaning |
|---|---|
| `per_unit` | each unit's `{seq, verdict: pass\|fail, reason}` (machine-readable tokens) |
| `settled_total` | total released so far (= rate × units paid) |
| `remainder_unspent` | budget never spent (`max_total − settled_total`) |
| `trace` | a replayable log the total can be reconstructed from |
| `verdict_signature` | Ed25519 signature over the receipt (set in the `signed` tier; `null` in `hash-only`) |
| `display` | the printable, plain-English transcript |

### `GET /health` — liveness
```
curl "<base>/health"
→ {"status":"ok","service":"settlement-verifier"}
```

### `GET /skill.md` — this document (served as markdown)

### `GET /pubkey` — the verifier's Ed25519 verdict-signing public key
```
curl "<base>/pubkey"
→ {"pubkey":"ed2412b5…","algo":"ed25519"}
```

### `GET /demo/settlement?case=good|bad` — self-contained match-vs-mismatch demo
```
curl "<base>/demo/settlement?case=bad"
→ { "per_unit":[ {"seq":0,"verdict":"pass","reason":"artifact-match"},
                 {"seq":1,"verdict":"fail","reason":"artifact-mismatch"} ],
    "settled_total":100, "remainder_unspent":400,
    "trace":[ "stream-open:…", "ack:…", "gate:…:pass", "tick:…", "gate:…:fail", "stream-close:…:artifact-mismatch" ],
    "verdict_signature":null,
    "display":"Settlement complete: deal 174b5d7a…\nRule: pay 100 per delivered item, only if it exactly matches what was promised.\n  Item 0: PAID - genuine, matched what was promised.\n  Item 1: NOT PAID - did not match what was promised; payment stopped here.\nTotal paid: 100 | Budget unspent: 400 | Status: FINISHED" }
```

### `POST /settle/open` — open a metered stream
Commit up front the sha256 of each unit you expect. `expected` maps `"<seq>" → <sha256>`; `task_id` must equal `ref`.
```
curl -X POST "<base>/settle/open" -H "Content-Type: application/json" -d '{
  "ref": "<deal-id>",
  "rate": 100,                       // released per verified unit
  "max_total": 500,                  // spending cap; the rest stays unspent
  "attestor": "hash-only",           // "hash-only" (default) or "signed"
  "committed_reference": {
    "criterion": "artifact_match",   // artifact_match | checksum | ack_received
    "task_id": "<deal-id>",          // must equal ref
    "expected": { "0": "<sha256-of-unit-0>", "1": "<sha256-of-unit-1>" }
  }
}'
→ SettleOut (stream opened; nothing settled yet)
```

### `POST /settle/advance` — submit one delivered unit
`payload_hex` = hex of the delivered chunk's raw bytes; `declared_checksum` = its sha256. A passing unit releases `rate`; a failing unit closes the stream so the remainder is never spent.
```
curl -X POST "<base>/settle/advance" -H "Content-Type: application/json" -d '{
  "ref": "<deal-id>",
  "unit": { "seq": 0, "payload_hex": "<hex-bytes>", "declared_checksum": "<sha256>" }
  // "attestation": {"signer_pubkey":"…","sig":"…"}   // required only when attestor="signed"
}'
→ SettleOut (this unit's verdict + running totals)
```

### `POST /settle/close` — finalize the stream
```
curl -X POST "<base>/settle/close" -H "Content-Type: application/json" -d '{ "ref": "<deal-id>" }'
→ SettleOut (totals frozen; verdict_signature set in the signed tier)
```

### `POST /verify` — re-check a returned verdict offline
Verifies the verifier's own Ed25519 signature over `{receipt, trace}` against the key at `/pubkey`. In `hash-only` there is no signature, so this returns `{"valid":false,"reason":"no-signature"}`; use the `signed` tier to get a verifiable `verdict_signature`.
```
curl -X POST "<base>/verify" -H "Content-Type: application/json" -d '{ "receipt": {…}, "trace": [ … ], "verdict_signature": "<hex>" }'
→ {"valid":true,"reason":"signature-valid"}
```

## How it works — the gate ladder (per delivered unit)

The plain-English `display` is a view of these machine-checked verdicts; the exact tokens (`artifact-match`, `artifact-mismatch`, …) stay in `per_unit[].reason` and in the `trace` for programmatic checking.

1. **L1 ack** — the unit arrived.
2. **L2 checksum** — `sha256(chunk)` equals the seller's declared checksum (constant-time compare; a null checksum never settles).
3. **L3 artifact_match** — `sha256(chunk)` equals the committed hash for that `seq`, **and** the deal `ref` is embedded in the payload.

`criterion` chooses how far the ladder runs: `artifact_match` = L1+L2+L3, `checksum` = L1+L2, `ack_received` = L1 only. L2 short-circuits L3, so a unit with an honest checksum for the **wrong** bytes still fails identity — the exact case a checksum alone cannot catch, and the reason `?case=bad` fails.

## Attestor tiers (the `attestor` flag)

- **`hash-only`** (default) — no fulfillment signature; identity and integrity come from the hash alone. Best for self-verifying deliverables the buyer can re-hash.
- **`signed`** — the delivery carries an Ed25519 attestation from a **non-payee** key; the verifier checks that signature against its trusted-key allow-list **before** releasing, and returns a `verdict_signature` over `{receipt, trace}` that anyone can re-check via `/verify`. This is signature *verification* against a published key — not key matching.
- **`zkpret` / `vlei`** — the same seam with a stronger proof. Out of scope here — contact ChainAIM.

## The guarantee

`billed ≤ rate × verified_units`, and the billed amount is reconstructable from the emitted `trace`, not from any hidden internal balance. Nothing that isn't delivered, intact, and identical to what was committed is ever paid — and in the `signed` tier every verdict ships with an offline-verifiable receipt.
