« [Home](Home.md)

# Decision Log Index

One-line pointers into `ref/inference-service-kb/docs/decisions.md` — the ADR-style log of what was
chosen, what was rejected, and why. Read the source file for full reasoning; this page exists so band
pages can link to a specific decision without duplicating it.

### D1 — Adopt an inference engine, don't build one {#d1}
vLLM as execution core; a four-tier optimisation ladder (config → wrappers → plugin surfaces → fork) that
most businesses never need past tier 1. See [Band 5](Band-5-Inference-Fleet.md).

### D2 — agentgateway consolidates bands 2–4 {#d2}
Chosen over LiteLLM, Envoy AI Gateway, Kong, Apigee — mostly on licensing gates and streaming-native
design. See [agentgateway](agentgateway.md).

### D3 — GIE is not optional and not deferred {#d3}
Round-robin wastes GPU capacity by scattering prefix caches and ignoring KV saturation. See
[Band 4](Band-4-Inference-Routing.md).

### D4 — Defer llm-d / Dynamo {#d4}
Disaggregation can degrade performance 20–30% on small/untuned workloads. Instrument the adoption trigger
now even though the feature is deferred. See [Band 4](Band-4-Inference-Routing.md).

### D5 — Drop OPA {#d5}
"Can tenant X call model Y" is a DB row and a boolean — not worth a policy engine at MVP. See
[Band 3](Band-3-Identity-Tenancy.md).

### D6 — Defer Keycloak; never put it in the request path {#d6}
Machine→API auth (hashed key lookup) and human→portal auth (sessions, MFA, SSO) are different problems.
See [Band 3](Band-3-Identity-Tenancy.md) and [Control Plane B](Control-Plane-B-Identity.md).

### D7 — Drop the dedicated Envoy edge tier at MVP {#d7}
agentgateway is already Envoy-class — one fewer hop. Do **not** also drop Cloudflare; volumetric DDoS has
no self-host answer. See [Band 2](Band-2-Ingress.md).

### D8 — Router-based load balancing, no physical LB {#d8}
MetalLB in BGP mode; DC routers ECMP-hash flows. Watch for the re-hashing gotcha on node-set changes. See
[Band 2](Band-2-Ingress.md).

### D9 — Valkey over Redis (weak preference, not a hazard) {#d9}
No copyleft, LF governance; ~90% command compatible with Redis either way. See
[Band 3](Band-3-Identity-Tenancy.md).

### D10 — Build the developer portal ourselves {#d10}
Kong's portal shows API-call analytics; our unit is tokens and money. See
[Control Plane A](Control-Plane-A-Billing.md).

### D11 — Token rate limiting is two-phase {#d11}
Estimate before dispatch → reserve → dispatch → amend with actual usage as tokens stream. Overshoot is
inherent and accepted. `failureMode` (open/closed) is a business decision per limit type. See
[Band 3](Band-3-Identity-Tenancy.md) and [agentgateway](agentgateway.md).

## Related

- [Home](Home.md)
- [Non-Negotiable Constraints](Non-Negotiable-Constraints.md)
- Full text: `ref/inference-service-kb/docs/decisions.md`
