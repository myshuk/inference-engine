« [Home](Home.md)

# agentgateway

*Apache 2.0 · Linux Foundation / AAIF · Rust · Gateway API*

**One process performing the functions of [Band 2](Band-2-Ingress.md), [Band 3](Band-3-Identity-Tenancy.md),
and [Band 4](Band-4-Inference-Routing.md) internally.** Replaces Kong, LiteLLM, and Envoy AI Gateway —
see [D2](Decision-Log-Index.md#d2) for why those were rejected.

> **Important nuance:** "one process" describes where the *policy decision logic* runs, not where every
> dependency lives. Postgres, Envoy RLS, and Valkey ([Band 3](Band-3-Identity-Tenancy.md)) remain genuinely
> separate deployments that agentgateway calls out to. Don't model agentgateway itself as three network
> hops — but don't assume its dependencies collapse into it either.

## Internal stages

### ① Frontend policies (connection level) — Band 2's function

- TLS termination, HTTP/2, TCP + protocol settings
- long idle timeouts, SSE passthrough, connection draining
- access logging, tracing

### ② Traffic policies (route / tenant level) — Band 3's function

- JWT · API key · basic auth · authorization rules · ExtAuthz
- Rate limiting — **local** in-process bucket + **remote** RLS over gRPC (see
  [Band 3](Band-3-Identity-Tenancy.md) for the full RLS/Valkey topology)
  - `type: requests` (RPM) and `type: tokens` (TPM)
  - two-phase: **estimate** before dispatch → **amend** with actual usage ([D11](Decision-Log-Index.md#d11)).
    ⚠ Confirmed against the `envoyproxy/ratelimit` proto: the RLS counter itself has no refund/decrement
    call, so "amend" only ever corrects the Kafka billing record
    ([Control Plane A](Control-Plane-A-Billing.md)) — the Valkey rate-limit bucket keeps the original
    `max_tokens`-sized reservation for the rest of its window. See
    [Band 3](Band-3-Identity-Tenancy.md) for the full explanation.
  - conditional limits (anon vs. authenticated, read vs. write)
  - ⚠ **`failureMode: failOpen | failClosed`** — open for rate limits (availability wins), closed for
    spend caps (unbounded financial exposure is the alternative). This is a business decision, not a
    default to leave unexamined.
- transformation, CORS, timeouts, retries
- spend-cap check against the cached counter

### ③ Backend policies (upstream level) — Band 4's function

- LLM provider handling, prompt guards / inline guardrails
- MCP + A2A tool federation, OpenAPI→MCP, per-tool RBAC
- OpenTelemetry token usage → metering (feeds [Control Plane A](Control-Plane-A-Billing.md))

## Policy model

**Policy is CEL expressions, not plugin code.** There is no plugin marketplace — any custom in-path logic
that CEL can't express goes to an ExtAuthz/ExtProc sidecar instead. Budget a day for the team to learn
CEL if unfamiliar.

The request/response **body is only snapshotted when a policy actually references it** — this is what
keeps streaming cheap; a policy that never touches the body never buffers it.

## ⚠ Wiring — the thing most likely to be gotten wrong

`HTTPRoute → AgentgatewayBackend → custom provider → InferencePool`

Routing an `HTTPRoute` straight to the `InferencePool` (skipping the custom provider) silently loses
token counting, which is billing data. There's no error, no alarm — usage numbers are just quietly wrong.

## Why agentgateway over the alternatives ([D2](Decision-Log-Index.md#d2))

| Rejected | Why |
|---|---|
| LiteLLM | Open-core — SSO beyond 5 users and the guardrails suite need a paid licence (~$3k–30k/yr) |
| Envoy AI Gateway | Same slot as LiteLLM — pick one, not both; agentgateway is Rust, streaming-native, multi-tenant by design |
| Kong | Kong OSS never included the developer portal (that's Kong Enterprise); agentgateway covers Kong OSS's data-plane job *and* adds token-aware limits Kong can't do natively |
| Apigee | Commercial, 5–6 figures/yr, cloud-hosted — wrong for on-prem GPUs |

**Caveats accepted:** newer project than the alternatives — pin versions and test token accounting on
every upgrade.

## Related

- [Band 2](Band-2-Ingress.md), [Band 3](Band-3-Identity-Tenancy.md), [Band 4](Band-4-Inference-Routing.md)
- [Non-Negotiable Constraints](Non-Negotiable-Constraints.md) — streaming and billing rules this component must uphold
- [D2](Decision-Log-Index.md#d2), [D11](Decision-Log-Index.md#d11)
