« [Home](Home.md)

# Band 3 — Identity, Tenancy, Counters

**Function:** know who's calling, what they're allowed to do, and how much of it they've done. Performed
internally by [agentgateway](agentgateway.md) (its "traffic policies" stage) — but with genuinely
separate backing components, since agentgateway being "one process" describes where the *policy
decision* is made, not where every dependency's state lives.

## Components

### PostgreSQL

*(PostgreSQL licence)* Orgs → projects → hashed API keys, plans, per-key limits, model allow-lists.

- ⚠ **agentgateway enforces, Postgres remembers.** agentgateway's CEL policies decide *whether* a request
  passes, but they evaluate against data they don't own — org/key/plan/allow-list records have to be
  created (portal signup, key rotation, plan changes) and survive restarts somewhere. That somewhere is
  Postgres, not agentgateway config and not Valkey (lossy by design, cache only).
- **This is our domain model, not the IdP's.** API auth is a key lookup (cached in Valkey), not an OAuth
  flow — machine-to-API auth and human-to-portal auth are different problems. See
  [D6](Decision-Log-Index.md#d6) and [Control Plane B](Control-Plane-B-Identity.md).
- **OPA was removed from this path.** "Can tenant X call model Y" is a DB row and a boolean — a policy
  engine for that adds a Rego learning curve and a hot-path call to replace three lines. Revisit only if
  policy genuinely outgrows code and must be identical across gateway/portal/batch API. See
  [D5](Decision-Log-Index.md#d5).

### Envoy Rate Limit Service (RLS) → Valkey

*(Apache 2.0 / BSD-3, Linux Foundation)* agentgateway calls RLS over gRPC; Valkey holds the shared state:

- RPM + TPM token buckets
- cached spend counters
- idempotency keys

**Topology detail worth being explicit about:** this is actually three physical components, not one.
agentgateway's traffic-policy layer holds a **local** in-process bucket for fast-path rejection, and
separately calls a **remote** RLS service over gRPC for the shared, cross-replica view. RLS itself is
typically the `envoyproxy/ratelimit` reference implementation — it owns the descriptor/limit-matching
rules and does the check-and-increment logic, but the counters it increments live in Valkey. So: agentgateway
(client) → RLS (rules + logic) → Valkey (storage) is three separate deployments cooperating to perform one
band's function.

- ⚠ **Bill from usage events, never from these counters.** Valkey counters here are lossy by design
  (that's the tradeoff for speed). The Kafka usage-event stream in
  [Control Plane A](Control-Plane-A-Billing.md) is the billing record of truth. See
  [Non-Negotiable Constraints](Non-Negotiable-Constraints.md).
- **Why Valkey over Redis:** permissive licence (BSD-3, no copyleft at all) plus Linux Foundation
  governance, so the 2024 Redis relicensing surprise can't recur here. A weak preference, not a hazard —
  Redis 8 added AGPLv3 (OSI open source again) and would be fine too; the two are ~90% command
  compatible. See [D9](Decision-Log-Index.md#d9).
- **Two-phase rate limiting** (estimate → dispatch → amend) and the **failOpen / failClosed** split
  (open for rate limits, closed for spend caps) are configured here — see
  [D11](Decision-Log-Index.md#d11) for the full pattern and the overshoot tradeoffs it accepts.
- ⚠ **The "amend" half is billing-only — it cannot correct this counter.** Confirmed against the
  `envoyproxy/ratelimit` proto: `RateLimitRequest.hits_addend` (uint32) lets the *estimate* call report a
  variable cost, but it's unsigned and `RateLimitResponse` has no refund/decrement field — `ShouldRateLimit`
  is a single atomic check-and-increment with no compensating call. So once the `max_tokens` estimate is
  reserved here, this Valkey counter holds that reservation for the rest of its window regardless of what
  the model actually generated; "amend with actual usage" only ever means pushing the true count to Kafka
  in [Control Plane A](Control-Plane-A-Billing.md). Do not build a Valkey-bypass decrement to true this up —
  that splits authority over the same counter between RLS and a second writer. Overshoot here is
  structural, bounded only by window refill, not something a later step reduces.

## Related

- [agentgateway](agentgateway.md)
- [Control Plane A — Billing](Control-Plane-A-Billing.md) (spend state flows back here → Valkey)
- [Control Plane B — Identity](Control-Plane-B-Identity.md) (human auth, deliberately separate from this band)
- [D5](Decision-Log-Index.md#d5), [D6](Decision-Log-Index.md#d6), [D9](Decision-Log-Index.md#d9), [D11](Decision-Log-Index.md#d11)
