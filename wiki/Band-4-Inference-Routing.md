« [Home](Home.md)

# Band 4 — Inference Routing

**Function:** decide *where* a request goes among the vLLM replicas. Performed internally by
[agentgateway](agentgateway.md) (its "backend policies" stage), which delegates the actual replica
selection to the Gateway API Inference Extension.

## Components

### Gateway API Inference Extension (GIE) + llm-d-router

*(Apache 2.0 · Kubernetes SIG, with the Endpoint Picker now maintained in a sibling repo — see below)*

⚠ **Mid-project split, confirmed hands-on while building the local POC (see `poc/` in this repo):** GIE
used to own the whole stack — `InferencePool`, the Endpoint Picker (EPP), `InferenceObjective`, and
`InferenceModelRewrite`. It has since split: **GIE proper now only ships `InferencePool` and a
stripped-down reference EPP for conformance testing.** The Endpoint Picker you actually want to run, plus
`InferenceObjective` and `InferenceModelRewrite`, moved to a new repo, **`llm-d/llm-d-router`**, under a
**new API group, `llm-d.ai`** (not `inference.networking.k8s.io`). The POC deploys the real
`llm-d-router` EPP, not GIE's reference one — see `poc/README.md` Phase 2 and `poc/epp_scoring.md`.

- **InferencePool** (still GIE, `inference.networking.k8s.io/v1`) groups vLLM replicas into a routable
  backend. **Graduated to v1 (GA) as of v1.0.0** — this is a stable CRD, not an alpha one, which makes
  D3's "not deferred" position lower-risk than it looked when this decision was first made.
- **Endpoint Picker (EPP)** — now `llm-d/llm-d-router`'s, not GIE's — selects the replica by live
  KV-cache utilisation, pending queue length, and prefix-cache affinity, via a configurable weighted
  scoring pipeline (filter → score → pick), not round robin. **Verified hands-on, plugin names and metric
  names confirmed against the actual running config** (current names are lowercase-hyphenated, not the
  `PascalCase` names below):
  - `queue-scorer` — reads `vllm:num_requests_waiting`; score = `(maxQueue - queue) / (maxQueue - minQueue)`
    across the current candidate set (relative, not absolute).
  - `kv-cache-utilization-scorer` — reads `vllm:kv_cache_usage_perc`; score = `1 - kvCacheUsagePercent`
    (absolute, no normalization against peers).
  - `prefix-cache-scorer` — scores by the EPP's own internal prefix-hash tracking (approximate, or precise
    via a ZMQ KV-events feed from vLLM), not a scraped metric.
  - Per-scorer scores are combined by a **plain weighted sum** (`score × configured weight`, accumulated
    across all scorers), then `max-score-picker` takes the highest total. Full worked example with real
    numbers in `poc/epp_scoring.md`.
- **InferenceObjective** (now `llm-d.ai/v1alpha1`, via `llm-d-router` — not GIE's own API) — carries only a
  `Priority` field today (SLO attainment is planned but not yet implemented). A request is tied to one via
  the `x-gateway-inference-objective` header. **This is a plausible mechanism for the open
  [SLA tiers question](Home.md#open-questions)** — worth prototyping against before inventing a bespoke
  priority scheme.
- **InferenceModelRewrite** (now `llm-d.ai/v1alpha1`, via `llm-d-router`) — a CRD for model-name matching
  and optional rewriting within an `InferencePool` (e.g. mapping a customer-facing model alias to the
  actual served model/adapter). Not yet used anywhere in our design; flagging it here so it's not missed
  later.
- agentgateway is a conformant GIE gateway. Confirmed on the official implementations page (not just
  agentgateway's own claim) — NGINX Gateway Fabric is conformant too, and Istio support is tracked but not
  yet complete, as alternatives if agentgateway ever needs replacing. **Also confirmed hands-on that
  agentgateway's `InferencePool` support is gated behind a feature flag defaulted off
  (`inferenceExtension.enabled`, undocumented as a hard requirement anywhere obvious upstream at the time
  of writing) — budget for this if standing up agentgateway fresh.**

**Why this exists (not optional, not deferred — [D3](Decision-Log-Index.md#d3)):** round-robin scatters
prefix caches across replicas and ignores KV saturation, silently wasting a large fraction of GPU
capacity. GIE is one Deployment plus a CRD, works with plain aggregated vLLM, and doesn't require any
scheduling intelligence beyond what's described above.

**Action item from the decision log:** measure the counterfactual — run round-robin against real traffic
for a period and compare prefill token counts and TTFT. That number is what justifies keeping this band.

⚠ **Wiring matters:** `HTTPRoute → AgentgatewayBackend → custom provider → InferencePool`. Routing an
`HTTPRoute` straight to the `InferencePool` silently loses token counting, which is billing data. See
[Non-Negotiable Constraints](Non-Negotiable-Constraints.md).

### llm-d's advanced scheduling *or* NVIDIA Dynamo — Phase 2, deferred

⚠ **The adoption boundary here shifted with the GIE/llm-d-router split above.** `llm-d-router` (the EPP
host) is **no longer a deferred, optional adoption** — it's already required just to get basic EPP
routing at all, since GIE's own reference EPP is explicitly not meant for production use. What's *still*
deferred per [D4](Decision-Log-Index.md#d4) is llm-d's advanced scheduling intelligence:

- prefill / decode disaggregation
- tiered KV prefix cache (GPU → CPU → NVMe)
- workload-aware autoscaling

Confirmed hands-on: `llm-d-router` already ships sample EPP configs (`pd-epp-config.yaml`) with
`prefill-filter`/`decode-filter`/`disagg-profile-handler` plugins for P/D disaggregation, wired into the
*same* EPP deployment this band already runs. So adopting this is now more likely a **config change to
the EPP we already have** (add/enable those plugins, `schedulingProfiles` for `prefill`/`decode`) than a
separate project adoption — worth re-confirming against the current `llm-d-router` docs before treating
this as a bigger lift than it may actually be.

**Still deferred deliberately ([D4](Decision-Log-Index.md#d4)):** prefill/decode disaggregation can
*degrade* performance 20–30% on small or untuned workloads, because KV has to physically move between
GPUs — for short prompts or decode-side cache hits, local prefill is faster. Adopting this also means
running two GPU pools with independent scaling, which is real operational complexity.

- **Adoption trigger (instrument now, before you need it):** P99 decode latency spikes correlating with
  long-prompt arrivals. That pattern means prefill is interfering with decode. Add a Grafana panel for it
  today, in [Control Plane D](Control-Plane-D-Observability.md), even though the feature itself is
  deferred.
- **If/when we adopt: prefer llm-d's own P/D config over Dynamo.** It's the same EPP we already run, so
  it extends what's in place rather than replacing it. Choose Dynamo instead only if we standardise on
  TensorRT-LLM, want to mix engines per phase, or reach NVL-scale hardware — Dynamo brings its own Smart
  Router, which would displace the EPP and our existing gateway wiring.

### GIE + llm-d-router's baseline EPP vs. llm-d's advanced scheduling/Dynamo — the distinction that matters

The Endpoint Picker (whether running plain queue/KV-cache scoring, or with P/D-aware plugins enabled)
decides **where a request goes** among existing destinations. Dynamo's Smart Router decides **what the
destinations are** (e.g., splitting a single logical replica into separate prefill and decode pools) via
its own routing layer, not the EPP. They're complementary in principle, not competing — but note that
llm-d's own P/D disaggregation plugins run *inside* the same EPP this band already deploys (see above),
whereas Dynamo would displace that EPP with its own Smart Router entirely.

## Related

- [agentgateway](agentgateway.md)
- [Band 5 — Inference Fleet](Band-5-Inference-Fleet.md) — the Endpoint Picker scrapes metrics from here
- [Control Plane D — Observability](Control-Plane-D-Observability.md) — the P99 decode-latency panel to add now
- [D3](Decision-Log-Index.md#d3), [D4](Decision-Log-Index.md#d4)
- `poc/README.md` and `poc/epp_scoring.md` in this repo — hands-on validation of this band against a real
  (if scaled-down) `InferencePool` + `llm-d-router` EPP deployment
