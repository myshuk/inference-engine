# GPU Inference Service — Architecture Wiki

Source of truth: `ref/inference-service-kb/CLAUDE.md`, `docs/decisions.md`, and the diagram
`assets/inference-stack-v3.drawio`. This wiki restructures that material one page per band/component
for easier browsing. If this wiki and the source docs ever disagree, the source docs win — update this
wiki to match, not the other way around.

## What we're building

Inference-as-a-service on GPUs we own in our own datacenter. Customers call an OpenAI-compatible API,
we serve tokens, we bill per token. **Core thesis:** run vLLM as the execution core; build product
differentiation in the platform layer around it. Optimisation starts as engine *configuration*, not code.

## Current stage

Pre-MVP. Nothing deployed. GPU inventory not yet documented — this blocks TP sizing, model lineup, and
cost-per-token modelling. See [Open Questions](#open-questions) below.

## How to read this wiki

The stack is a **layer model** — bands are *functions applied in order*, not necessarily separate network
hops. Notably, **agentgateway is one process that internally performs the functions of bands 2–4** — see
its own page for why it still has genuinely separate dependencies (Postgres, Envoy RLS, Valkey) even
though it isn't three physical hops.

| # | Page | Function |
|---|---|---|
| 1 | [Band 1 — Clients](Band-1-Clients.md) | API contract: OpenAI-schema, streaming, `/v1`, logging promise |
| 2 | [Band 2 — Ingress, DDoS, TLS](Band-2-Ingress.md) | Cloudflare + MetalLB/BGP |
| 3 | [Band 3 — Identity, Tenancy, Counters](Band-3-Identity-Tenancy.md) | Postgres + Envoy RLS + Valkey |
| 4 | [Band 4 — Inference Routing](Band-4-Inference-Routing.md) | GIE InferencePool + Endpoint Picker (+ llm-d later) |
| 5 | [Band 5 — Inference Fleet](Band-5-Inference-Fleet.md) | vLLM on Kubernetes |
| 6 | [Band 6 — GPU Infrastructure](Band-6-GPU-Infrastructure.md) | GPU Operator, NCCL/NVLink, Kueue, DCGM |

Cutting across bands 2–4:

- [agentgateway](agentgateway.md) — the single process performing bands 2–4

Side control planes (drawn as the right-hand column in the diagram, not part of the banded request path):

| | Page | Purpose |
|---|---|---|
| A | [Control Plane A — Billing](Control-Plane-A-Billing.md) | Developer portal, Kafka, ClickHouse, rating, Stripe |
| B | [Control Plane B — Identity](Control-Plane-B-Identity.md) | Keycloak (human auth) — deferred |
| C | [Control Plane C — Model Registry](Control-Plane-C-Model-Registry.md) | Weight storage, quantisation, fast load |
| D | [Control Plane D — Observability & Platform](Control-Plane-D-Observability.md) | Prometheus/Grafana, KEDA, GitOps |

Cross-cutting references:

- [MVP Scope & Roadmap](MVP-Scope.md) — what ships first, what's deferred, what's explicitly rejected
- [Non-Negotiable Constraints](Non-Negotiable-Constraints.md) — the rules that hold regardless of band
- [Decision Log Index](Decision-Log-Index.md) — one-line pointers into `docs/decisions.md` (D1–D11)

## Open Questions

- [ ] **GPU inventory**: model, count per node, NVLink vs PCIe, total VRAM. Blocks TP sizing, model
      lineup, and cost-per-token modelling.
- [ ] Model lineup + licence review (biggest legal risk — see [Band 5](Band-5-Inference-Fleet.md))
- [ ] SLA tiers and target latency percentiles — GIE's `InferenceObjective` CRD (`Priority` field,
      SLO attainment planned) is a plausible mechanism; see [Band 4](Band-4-Inference-Routing.md)
- [ ] Benchmark plan for engine config tuning
- [ ] Cost-per-million-token model per GPU config
