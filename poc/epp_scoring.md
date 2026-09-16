# EPP scoring — how the final pod actually gets picked

Reference notes on the exact selection algorithm used by `llm-d-router`'s EPP for this POC's
`vllm-mock-pool` (`poc/k8s/llm-d-epp-overlay/epp-configmap.yaml`). Written after reading the actual
`llm-d/llm-d-router` source (not just the docs) to confirm the precise formulas — companion to
[`README.md`](README.md) (the plan) and [`exeReadme.md`](exeReadme.md) (what was run); this file is *how
the thing we built actually decides*.

## Our config

```yaml
apiVersion: llm-d.ai/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: prefix-cache-scorer
- type: queue-scorer
- type: kv-cache-utilization-scorer
- type: decode-filter
- type: max-score-picker
- type: single-profile-handler
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: decode-filter
  - pluginRef: max-score-picker
  - pluginRef: prefix-cache-scorer
    weight: 1
  - pluginRef: queue-scorer
    weight: 2
  - pluginRef: kv-cache-utilization-scorer
    weight: 2
```

## The three-stage pipeline

Per request, `SchedulerProfile.Run()` (`pkg/epp/scheduling/scheduler_profile.go`) runs exactly three
stages in order, against the pool's current candidate pods:

1. **Filter** — narrows the candidate list. We only have `decode-filter` (a P/D-disaggregation-aware
   filter that restricts candidates to "decode" role pods) — a no-op here since neither mock is labeled
   with a P/D role, so both mocks always pass through to scoring.
2. **Score** — every remaining scorer plugin runs against the *same* filtered candidate set. Each
   plugin's per-endpoint score is multiplied by its configured weight and accumulated:
   ```go
   // pkg/epp/scheduling/scheduler_profile.go, runScorerPlugins
   weightedScorePerEndpoint[endpoint] += enforceScoreRange(score) * scorer.Weight()
   ```
   This is a plain weighted sum across all configured scorers — not a normalized/averaged blend, and not
   independent per-scorer decisions.
3. **Pick** — `max-score-picker` (`pkg/epp/framework/plugins/scheduling/picker/maxscore/picker.go`)
   shuffles the candidates first (for random tie-breaking when scores are exactly equal), sorts by
   accumulated score descending, and returns the top `maxNumOfEndpoints` (default `1`).

## The scorer formulas — not symmetric

**`queue-scorer`** (`pkg/epp/framework/plugins/scheduling/scorer/queuedepth/queue.go`) — relative,
normalized against *only the current candidate set*, reading `vllm:num_requests_waiting`:

```
score(endpoint) = (maxQueue - queue(endpoint)) / (maxQueue - minQueue)
```

Shortest queue among the current candidates always scores exactly `1.0`, longest always scores exactly
`0.0`, others scale linearly between. If every candidate is tied, everyone gets a neutral `1.0`.

**`kv-cache-utilization-scorer`**
(`pkg/epp/framework/plugins/scheduling/scorer/kvcacheutilization/kvcache_utilization.go`) — absolute, no
normalization against peers at all, reading `vllm:kv_cache_usage_perc`:

```
score(endpoint) = 1 - kvCacheUsagePercent
```

This distinction matters in practice: `queue-scorer`'s scale is always stretched to fill `[0, 1]`
regardless of how close the real queue depths are — with only two candidates, even a 1-vs-2 waiting
difference becomes a full `1.0` vs `0.0` swing. `kv-cache-utilization-scorer`'s scale reflects the actual
magnitude of load and doesn't get more extreme just because the candidate pool is small. With more pods
in a real pool, `queue-scorer`'s spread would be less polarized per-pod since min/max is computed across
the whole field, not just two.

**`prefix-cache-scorer`** — not covered in exact formula here; scores based on EPP's own internal
tracking (approximate hash-based, or precise via a ZMQ KV-events feed from vLLM) of which pod has already
seen a matching token prefix. Kept at weight `1` (vs. `2` for the other two) deliberately, so repeated
identical test prompts wouldn't let prefix affinity compete with the queue/KV-cache signal being
demonstrated — see `poc/k8s/llm-d-epp-overlay/epp-configmap.yaml`'s own comment.

## Worked example — our actual mocks

Config from `poc/k8s/mock-servers/deployment.yaml`: `vllm-mock-a` starts at `waiting=0, kv=0.05`;
`vllm-mock-b` starts at `waiting=50, kv=0.9`.

| | `queue-scorer` (×2) | `kv-cache-utilization-scorer` (×2) | `prefix-cache-scorer` (×1) | total |
|---|---|---|---|---|
| mock-a | 1.0 → **2.0** | 0.95 → **1.9** | ~neutral, same both sides | ~**3.9+** |
| mock-b | 0.0 → **0.0** | 0.10 → **0.2** | ~neutral, same both sides | ~**0.2+** |

mock-a wins by a wide margin regardless of `prefix-cache-scorer`'s exact value, since that value is
identical on both sides for fresh mocks with no prior request history. This matches what was actually
observed: 5/5 test requests routed to `vllm-mock-a`. Flipping both mocks' load via their `/control`
endpoints and rerunning flipped the result to 5/5 `vllm-mock-b` — with only two candidates, both scorers
move to their extremes almost immediately on any load difference, so the pick tracks the metrics
deterministically rather than being sticky or random.

## Wiki correction, now applied

`wiki/Band-4-Inference-Routing.md` used to document `QueueScorer`/`KVCacheUtilizationScorer` (the
pre-split GIE EPP's plugin names) without a specific metric name (it just said "KV-cache utilisation,
queue depth" generically — an earlier draft of this POC's own docs mistakenly claimed the wiki cited
`vllm:gpu_cache_usage_perc` specifically; it never did). The wiki has now been updated with the real,
current plugin type strings (lowercase-hyphenated: `queue-scorer`, `kv-cache-utilization-scorer`,
`prefix-cache-scorer`) and the real metric name, `vllm:kv_cache_usage_perc`.
