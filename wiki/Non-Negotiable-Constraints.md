« [Home](Home.md)

# Non-Negotiable Constraints

These hold regardless of band — any change that violates one of these is a design flaw, not a tradeoff to
weigh.

1. **Streaming is the default path.** Every hop must support long-lived unbuffered SSE: no response
   buffering, no compression on `text/event-stream`, idle timeouts measured in minutes, connection
   draining on deploy. If a change breaks streaming, it's a P0. Touches
   [Band 1](Band-1-Clients.md), [Band 2](Band-2-Ingress.md) (Cloudflare), [agentgateway](agentgateway.md),
   and [Control Plane D](Control-Plane-D-Observability.md) (KEDA scale-in).

2. **Bill from usage events, never from rate-limit counters.** Valkey counters
   ([Band 3](Band-3-Identity-Tenancy.md)) are lossy by design — that's an acceptable tradeoff for
   fast-path rejection, but it makes them unfit as a billing source. Kafka events
   ([Control Plane A](Control-Plane-A-Billing.md)) are the record of truth.

3. **Route via `AgentgatewayBackend` → custom provider → `InferencePool`.** A raw `HTTPRoute` straight to
   the `InferencePool` silently loses token counting, which is billing data. See
   [Band 4](Band-4-Inference-Routing.md) and [agentgateway](agentgateway.md).

4. **Health probes must test generation ability, not process liveness.** A CUDA wedge leaves the process
   reporting healthy while unable to generate tokens. See [Band 6](Band-6-GPU-Infrastructure.md).

5. **OpenAI schema compatibility is a product requirement, not a nicety.** Match request/response shapes
   exactly, including `usage`. Extensions are additive fields only. See [Band 1](Band-1-Clients.md).

6. **`/v1` in every path**, from day one.

7. **Prefer permissive licences.** OpenTofu not Terraform, OpenBao not Vault, Valkey not Redis, Kafka not Redpanda, Ceph not MinIO. See `docs/licensing.md` and the relevant component pages
   ([Band 3](Band-3-Identity-Tenancy.md), [Control Plane A](Control-Plane-A-Billing.md),
   [Control Plane C](Control-Plane-C-Model-Registry.md), [Control Plane D](Control-Plane-D-Observability.md)).

## Related

- [Home](Home.md)
- [Decision Log Index](Decision-Log-Index.md)
