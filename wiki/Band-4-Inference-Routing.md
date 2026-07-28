« [Home](Home.md)

# Band 4 — Inference Routing

**Function:** decide *where* a request goes among the vLLM replicas. Performed internally by
[agentgateway](agentgateway.md) (its "backend policies" stage), which delegates the actual replica
selection to the Gateway API Inference Extension.

## Components

### Gateway API Inference Extension (GIE)

*(Apache 2.0 · Kubernetes SIG)*

- **InferencePool** groups vLLM replicas into a routable backend. **Graduated to v1 (GA) as of v1.0.0** —
  this is a stable CRD, not an alpha one, which makes D3's "not deferred" position lower-risk than it
  looked when this decision was first made.
- **Endpoint Picker (EPP)** selects the replica by live KV-cache utilisation, pending queue length, and
  active LoRA adapters — not round robin. Confirmed against the current docs: the scoring plugins are
  literally named `QueueScorer` (scores inversely proportional to queue depth) and
  `KVCacheUtilizationScorer` (scores inversely proportional to KV-cache utilisation), plus a prefix-cache
  affinity plugin.
- **InferenceObjective** — a newer CRD that **replaces the old `InferenceModel` name in the v1 API**.
  Currently carries only a `Priority` field (SLO attainment is planned but not yet implemented). A request
  is tied to one via the `x-gateway-inference-objective` header. **This is a plausible mechanism for the
  open [SLA tiers question](Home.md#open-questions)** — worth prototyping against before inventing a
  bespoke priority scheme.
- **InferenceModelRewrite** — a CRD for model-name matching and optional rewriting within an
  `InferencePool` (e.g. mapping a customer-facing model alias to the actual served model/adapter). Not
  yet used anywhere in our design; flagging it here so it's not missed later.
- agentgateway is a conformant GIE gateway. Confirmed on the official implementations page (not just
  agentgateway's own claim) — NGINX Gateway Fabric is conformant too, and Istio support is tracked but not
  yet complete, as alternatives if agentgateway ever needs replacing.

**Why this exists (not optional, not deferred — [D3](Decision-Log-Index.md#d3)):** round-robin scatters
prefix caches across replicas and ignores KV saturation, silently wasting a large fraction of GPU
capacity. GIE is one Deployment plus a CRD, works with plain aggregated vLLM, and doesn't require any
scheduling intelligence beyond what's described above.

**Action item from the decision log:** measure the counterfactual — run round-robin against real traffic
for a period and compare prefill token counts and TTFT. That number is what justifies keeping this band.

⚠ **Wiring matters:** `HTTPRoute → AgentgatewayBackend → custom provider → InferencePool`. Routing an
`HTTPRoute` straight to the `InferencePool` silently loses token counting, which is billing data. See
[Non-Negotiable Constraints](Non-Negotiable-Constraints.md).

### llm-d *or* NVIDIA Dynamo — Phase 2, deferred

*(Apache 2.0)* Scheduling intelligence that would sit behind the EPP:

- prefill / decode disaggregation
- tiered KV prefix cache (GPU → CPU → NVMe)
- workload-aware autoscaling

**Deferred deliberately ([D4](Decision-Log-Index.md#d4)):** prefill/decode disaggregation can *degrade*
performance 20–30% on small or untuned workloads, because KV has to physically move between GPUs — for
short prompts or decode-side cache hits, local prefill is faster. Adopting this also means running two
GPU pools with independent scaling, which is real operational complexity.

- **Adoption trigger (instrument now, before you need it):** P99 decode latency spikes correlating with
  long-prompt arrivals. That pattern means prefill is interfering with decode. Add a Grafana panel for it
  today, in [Control Plane D](Control-Plane-D-Observability.md), even though the feature itself is
  deferred.
- **If/when we adopt: prefer llm-d.** It's built on GIE and shares the same Endpoint Picker, so it
  extends what we already run. Choose Dynamo instead only if we standardise on TensorRT-LLM, want to mix
  engines per phase, or reach NVL-scale hardware — Dynamo brings its own Smart Router, which would
  displace the EPP and our existing gateway wiring.

### GIE vs. llm-d/Dynamo — the distinction that matters

GIE decides **where a request goes** among existing destinations. llm-d/Dynamo decide **what the
destinations are** (e.g., splitting a single logical replica into separate prefill and decode pools).
They're complementary, not competing — GIE is the router; llm-d/Dynamo would be a policy that changes what
GIE routes to.

## Related

- [agentgateway](agentgateway.md)
- [Band 5 — Inference Fleet](Band-5-Inference-Fleet.md) — GIE scrapes metrics from here
- [Control Plane D — Observability](Control-Plane-D-Observability.md) — the P99 decode-latency panel to add now
- [D3](Decision-Log-Index.md#d3), [D4](Decision-Log-Index.md#d4)
