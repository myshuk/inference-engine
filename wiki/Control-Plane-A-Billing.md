« [Home](Home.md)

# Control Plane A — Customer-Facing Control Plane & Billing

Not part of the banded request path — this is the side system that turns usage into invoices.

## Components

### Developer portal — we build this

Signup, org creation, key generate/rotate/revoke, usage by model and day, spend vs. cap, payment method,
invoices, docs + quickstart. Later: teams, roles, projects.

**Why build it ourselves ([D10](Decision-Log-Index.md#d10)):** Kong's portal shows API-call analytics —
wrong unit for us. Our product's unit is tokens and money, so we need usage-by-model, spend-vs-cap,
invoices, and credit top-ups regardless of what any gateway vendor's portal shows. A second, differently
numbered portal would just confuse customers.

**Sequencing:** docs site (Mintlify/Docusaurus) + manual key provisioning for design partners first →
self-serve signup and key management once manual provisioning starts costing real time. See
[MVP Scope](MVP-Scope.md).

### Shared usage-event infrastructure: Kafka + ClickHouse

*(Apache 2.0 — Kafka avoids Redpanda's BSL)* **One Kafka topic, one ClickHouse cluster — not one pair per
consumer.** It's tempting to read this as four sequential systems (Kafka → ClickHouse → rating engine →
Stripe), but that overstates how much infrastructure this actually needs.

- **Kafka** — usage event bus; one event per request carrying prompt / cached / completion tokens.
  **This is the billing record of truth.** Never bill from the Valkey rate-limit counters in
  [Band 3](Band-3-Identity-Tenancy.md) — those are lossy by design. See
  [Non-Negotiable Constraints](Non-Negotiable-Constraints.md).
- **ClickHouse** — usage warehouse fed by that same Kafka topic; per tenant/model/hour rollups powering
  the portal's dashboards directly.

⚠ **Both OpenMeter and Lago use Kafka + ClickHouse internally by default — don't run a second, dedicated
copy for the rating engine.** [OpenMeter's own architecture](https://openmeter.io/blog/how-openmeter-uses-clickhouse-for-usage-metering)
is explicitly ingest-via-Kafka → exactly-once Kafka Connect Sink → ClickHouse as long-term storage; Lago
uses ClickHouse as its event store at scale the same way. Their Helm/deploy charts bundle a dedicated
Kafka+ClickHouse pair, but that's documented as a **development convenience**, not a production
requirement — configure the rating engine to consume from the *same* Kafka topic and ClickHouse cluster
this section already stood up, rather than duplicating both.

### OpenMeter / Lago + Stripe

The genuinely distinct piece: billing *domain logic* on top of the shared storage above — rating (price
books per tier, **cached-token discount**, credits, spend caps) then payment. This is real, separate work
that a query engine doesn't provide: proration, credit ledgers, invoice generation, dunning. Spend state
is pushed back to Valkey ([Band 3](Band-3-Identity-Tenancy.md)) for near-real-time cap enforcement — note
this is a one-way sync *from* the authoritative billing pipeline *to* the fast-path cache, not the other
way around.

## Data flow

```
agentgateway (OTel token usage) → Kafka ─┬→ ClickHouse (portal dashboards)
                                          └→ rating engine (OpenMeter/Lago, same Kafka + ClickHouse) → Stripe
                                                              │
                                                              ▼
                                            spend state → Valkey (Band 3, cache only)
```

Three jobs (transport, analytics query, billing domain logic) — two pieces of shared infrastructure
(Kafka, ClickHouse), not four independent systems.

## Related

- [Band 3 — Identity, Tenancy, Counters](Band-3-Identity-Tenancy.md) — receives the cached spend state
- [agentgateway](agentgateway.md) — emits the OTel token usage this pipeline consumes
- [D10](Decision-Log-Index.md#d10)
- [MVP Scope](MVP-Scope.md) — Kafka + ClickHouse metering is explicitly deferred past initial MVP
