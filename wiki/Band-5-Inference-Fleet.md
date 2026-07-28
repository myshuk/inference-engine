« [Home](Home.md)

# Band 5 — Inference Fleet

**Function:** where tokens are actually generated. This is the execution core of the whole product —
per [D1](Decision-Log-Index.md#d1), we adopt an existing engine here rather than building one.

## Components

Two different layers live in this band — they stack, they don't compete. You pick **one** engine per
deployment; the orchestration layer wraps whichever engine you picked.

### Execution engines — pick one, benchmark per model

These actually run the model and generate tokens: batching, attention/KV-cache management, quantisation.

#### vLLM — default

*(Apache 2.0)* Continuous batching, PagedAttention, automatic prefix caching, chunked prefill, TP/PP,
FP8 & AWQ quantisation, speculative decoding, LoRA. Exposes the metrics that
[Band 4](Band-4-Inference-Routing.md)'s Endpoint Picker scrapes (KV-cache utilisation, queue depth).

#### SGLang

*(Apache 2.0)* RadixAttention prefix reuse; often wins on agent and structured-output traffic.
**Benchmark per model, not per vendor** — the right engine can vary by workload, not just by company
preference.

#### TensorRT-LLM + Triton

*(Apache 2.0 / BSD-3; CUDA under NVIDIA EULA)* Maximum NVIDIA throughput via compiled engines, at the
cost of heavier build and ops overhead. Premium latency tier only — not the default path.

### Orchestration layer — wraps whichever engine you picked

This layer does not execute the model. It handles the Kubernetes deployment lifecycle around whatever
engine (vLLM, SGLang, or TensorRT-LLM) is running inside the pod.

#### KServe / Ray Serve + LeaderWorkerSet

*(Apache 2.0)* KServe/Ray Serve provide the serving CRDs: canary rollout, scale-to-zero. **LeaderWorkerSet
(LWS)** solves a problem the engines don't touch at all — it gang-schedules a multi-node TP/PP replica as
a single unit with all-or-nothing restart, needed because a partially restarted tensor-parallel group is
not a working replica.

## The optimisation ladder ([D1](Decision-Log-Index.md#d1))

Most businesses never need to pass tier 1. Don't reach tier 4 without revenue, a profiled bottleneck,
*and* a CUDA engineer on the team.

1. **Configuration** — batching params, cache sizes, quantisation choice, TP/PP layout
2. **Thin wrappers/sidecars** — logic that sits alongside the engine, doesn't touch its internals
3. **Engine plugin surfaces** — logits processors, LoRA adapters
4. **Forking the engine** — escalate before attempting this; see `docs/build-vs-buy.md`

## ⚠ Model weights are the licence risk that actually matters

We are reselling model output commercially — this is a different (and larger) legal exposure than the
infra-component licences tracked elsewhere in this wiki.

| Model family | Licence posture |
|---|---|
| Qwen | Apache 2.0 — clean |
| DeepSeek | MIT — clean |
| Llama | Community licence — MAU threshold, attribution, output-use restrictions |
| Mistral | Mixed — some models prohibit commercial use |
| Gemma | Use-policy limits |

**Legal review of the model lineup before launch** — tracked as an open question on [Home](Home.md), and
flagged in `docs/licensing.md` as the biggest legal risk in the whole project.

## Related

- [Band 4 — Inference Routing](Band-4-Inference-Routing.md) — consumes the metrics this band exposes
- [Band 6 — GPU Infrastructure](Band-6-GPU-Infrastructure.md) — what this band runs on
- [Control Plane C — Model Registry](Control-Plane-C-Model-Registry.md) — where weights and images come from
- [D1](Decision-Log-Index.md#d1)
