« [Home](Home.md)

# Control Plane D — Observability, Scaling & Platform

Not part of the banded request path — the operational backbone underneath everything else.

## Components

### Prometheus / VictoriaMetrics

*(Apache 2.0)* Scrapes vLLM + DCGM: TTFT, inter-token latency, tok/s, KV-cache utilisation, queue depth,
and **preemptions** (a preemption signal means the fleet is oversubscribed — worth its own alert).

### Grafana / Loki / Tempo + OpenTelemetry tracing

*(AGPLv3 — fine for internal use; get legal review before embedding any of this in the customer-facing
portal.)*

### KEDA

*(Apache 2.0)* Scales replica groups on queue depth and TTFT, not CPU — CPU utilisation is a meaningless
signal for a GPU-bound inference workload.

- ⚠ **Long termination grace periods, or scale-in truncates live streams.** This is the autoscaling
  analogue of the streaming constraint in
  [Non-Negotiable Constraints](Non-Negotiable-Constraints.md) — a scale-down decision must wait for
  in-flight SSE connections to drain, not just stop routing new ones.

### Argo CD + OpenTofu · OpenBao · Trivy / Falco

GitOps, secrets, supply chain.

- **OpenTofu, not Terraform** — Terraform is BUSL (not permissive).
- **OpenBao, not Vault** — Vault is BUSL.
- Every engine-tuning change is a reviewed Git commit — tuning is empirical, and regressions need to be
  bisectable. This is a repo-wide convention, not just a Band 5 practice.

## Related

- [Band 4 — Inference Routing](Band-4-Inference-Routing.md) — add the P99 decode-latency panel here as the llm-d/Dynamo adoption trigger
- [Non-Negotiable Constraints](Non-Negotiable-Constraints.md)
