« [Home](Home.md)

# MVP Scope & Roadmap

## MVP cut line — ship first

- Cloudflare → [agentgateway](agentgateway.md) → **GIE (InferencePool + Endpoint Picker)** → vLLM on K8s
  + GPU Operator ([Band 2](Band-2-Ingress.md)–[Band 6](Band-6-GPU-Infrastructure.md))
- Postgres + Valkey ([Band 3](Band-3-Identity-Tenancy.md))
- Prometheus/Grafana ([Control Plane D](Control-Plane-D-Observability.md))
- Docs site + manual key provisioning ([Control Plane A](Control-Plane-A-Billing.md))
- Stripe ([Control Plane A](Control-Plane-A-Billing.md))

⚠ **GIE is day-one, not deferred.** Round-robin without it scatters prefix caches and ignores KV
saturation, silently wasting GPU capacity — see [D3](Decision-Log-Index.md#d3) and
[Band 4](Band-4-Inference-Routing.md). It's cheap to add (one Deployment + a CRD, and agentgateway is
already a conformant GIE gateway), unlike llm-d/Dynamo below, which is a genuinely heavy lift and stays
deferred.

## Add as we grow — roughly in this order

1. Self-serve portal ([Control Plane A](Control-Plane-A-Billing.md))
2. Kafka + ClickHouse metering ([Control Plane A](Control-Plane-A-Billing.md))
3. KEDA autoscaling ([Control Plane D](Control-Plane-D-Observability.md))
4. Keycloak SSO ([Control Plane B](Control-Plane-B-Identity.md))
5. Dedicated Envoy edge tier ([Band 2](Band-2-Ingress.md))
6. llm-d / Dynamo ([Band 4](Band-4-Inference-Routing.md))

## Explicitly rejected

Kong, Apigee, LiteLLM, Envoy AI Gateway, OPA. See the [Decision Log Index](Decision-Log-Index.md) for the
reasoning behind each.

## Related

- [Home](Home.md) — open questions blocking further decisions
- [Decision Log Index](Decision-Log-Index.md)
