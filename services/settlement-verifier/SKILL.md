# Outcome-Verified Settlement Verifier

A generic, use-case-agnostic settlement verifier. Given a committed reference (a
`{seq: sha256}` map bound to a deal `ref`), it meters a payment stream and releases
`rate` per **verified** unit, refusing anything that is not delivered, intact, and
identical to what was committed. It proves **identity + integrity + delivery only** — all
business judgement (grade, quantity, date, schema) happens upstream and is frozen into the
committed hash before it ever reaches this service.

## Base URL

https://chainaim-outcome-verified-settlement-ext-production.up.railway.app
<!-- Hosted on Railway (Service C). LOCAL default was http://localhost:7016. Refresh the copy in OpenClaw whenever this URL changes. -->

## How the agent should use this

Call `GET /demo/settlement` and **print the `display` field verbatim.** It is a complete,
pre-formatted transcript of a self-contained settlement — no other input, no keys, no
human step. For the mismatch case, call `GET /demo/settlement?case=bad`.

Do not summarise or reformat `display`; print it exactly as returned.

## What the demo shows

- `?case=good` — every delivered unit matches its committed hash → each settles per tick.
- `?case=bad` — a matching unit settles, then a swapped unit (an honest checksum for the
  wrong bytes) passes the integrity check but **fails the identity check (L3)** → the
  stream closes and the remainder stays unspent.

The money property: `billed <= rate x verified_units`, and it is reconstructable from the
emitted `trace`, not from any internal balance.

## Endpoints

| Method + path | Purpose |
|---|---|
| `GET /health` | liveness |
| `GET /skill.md` | this document |
| `GET /pubkey` | the verifier's Ed25519 verdict-signing public key (hex) |
| `GET /demo/settlement?case=good\|bad` | self-contained match-vs-mismatch demo |
| `POST /settle/open` | open a stream: `{ref, rate, max_total, committed_reference, attestor}` |
| `POST /settle/advance` | submit one unit `{ref, unit, attestation?}` → settle or refuse |
| `POST /settle/close` | `{ref}` → final per-unit verdicts, totals, trace, verdict signature |
| `POST /verify` | offline re-check of a returned `{receipt, trace, verdict_signature}` |

## The gate ladder (per unit)

1. **L1 ack** — the unit arrived.
2. **L2 checksum** — `sha256(chunk)` equals the seller's declared checksum (constant-time;
   a null checksum never settles).
3. **L3 artifact_match** — `sha256(chunk)` equals the committed hash for that `seq`, and the
   deal `ref` is embedded in the payload.

`artifact_match` runs L1+L2+L3; `checksum` runs L1+L2; `ack_received` runs L1 only.

## Attestor tiers (one flag: `attestor`)

- `hash-only` (default) — no fulfillment signature; identity/integrity from the hash alone.
  Best for self-verifying deliverables (the buyer can recompute the hash).
- `signed` — the delivery carries an Ed25519 attestation from a **non-payee** key; the
  verifier checks that signature against its trusted-pubkey allow-list **before** releasing,
  and returns a `verdict_signature` over the receipt + trace. Signature **verification**
  against a published key — not key matching.
- `zkpret` / `vlei` — the same seam with a stronger proof. Out of scope here — contact ChainAIM.
