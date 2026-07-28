« [Home](Home.md)

# Band 1 — Clients

**Function:** just API calls in. No infrastructure of our own lives in this band — it's the contract we
promise to every caller. Four decisions live here, and they're load-bearing for everything downstream:

## The four decisions

1. **OpenAI-schema compatibility.** Match request/response shapes exactly, including the `usage` object.
   Extensions are additive fields only — never rename or repurpose an existing field. This is a product
   requirement, not a nicety: customers' existing SDKs and tooling must work unmodified.
2. **Streaming is the default path.** Every hop downstream (Cloudflare, agentgateway, GIE, vLLM) must
   support long-lived, unbuffered SSE. See [Non-Negotiable Constraints](Non-Negotiable-Constraints.md) —
   breaking streaming is a P0 incident, not a bug.
3. **`/v1` in every path, from day one.** Avoids a breaking migration later.
4. **Whether we promise not to log prompts.** A policy decision with legal/trust implications — affects
   what we can store, for how long, and what we tell customers in the ToS.

## Why this band matters architecturally

Everything below Band 1 exists to serve this contract without violating it. In particular:

- The OpenAI schema requirement is why [Band 4](Band-4-Inference-Routing.md)'s routing note is so
  emphatic about wiring `HTTPRoute → AgentgatewayBackend → custom provider → InferencePool` — a naive
  route straight to the `InferencePool` silently drops token counting, which breaks the `usage` field
  the schema promises.
- The streaming requirement is why [Cloudflare](Band-2-Ingress.md) must disable buffering/compression on
  `text/event-stream`, why [agentgateway](agentgateway.md) only snapshots the request body when a policy
  actually references it, and why [KEDA](Control-Plane-D-Observability.md) scale-in needs long
  termination grace periods.

## Related

- [Non-Negotiable Constraints](Non-Negotiable-Constraints.md)
- [Band 2 — Ingress](Band-2-Ingress.md)
- [agentgateway](agentgateway.md)
