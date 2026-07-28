« [Home](Home.md)

# Control Plane C — Model Registry & Artifacts

Not part of the banded request path — this is the supply chain that gets a model from "exists upstream"
to "ready to be pulled by a Band 5 replica." It is not responsible for routing or serving decisions —
which replica serves which request is Band 4's job, not this one.

## Components — three pipeline stages, in order

### ① Entry — HF mirror + quantisation pipeline

Where a model enters the system. Pulls the model from upstream (Hugging Face), builds the FP8 / AWQ
quantised variants actually served, records evals **and the licence terms per model**.

This is the **operational enforcement point** for the licensing risk flagged in
[Band 5](Band-5-Inference-Fleet.md#-model-weights-are-the-licence-risk-that-actually-matters) (Llama's
MAU threshold, Mistral's mixed commercial-use terms, etc.) — since we're reselling model output
commercially, this pipeline is where "did we check the licence before shipping this model" actually gets
enforced, not just noted in a doc somewhere.

### ② Storage — Ceph / SeaweedFS

*(LGPL / Apache)* Where the weights live at rest — an S3-compatible store for the safetensors shards the
quantisation pipeline produces.

**Why not MinIO:** a licensing dodge, specifically. MinIO is AGPL, and its community edition has had
features stripped out over time. Ceph/SeaweedFS avoid both the copyleft question and the feature-gating
risk.

### ③ Delivery — Run:ai Model Streamer / JuiceFS + Harbor

*(Apache 2.0)* How the weights and engine actually reach a running replica — two different artifacts,
two different concerns:

- **Model Streamer / JuiceFS** solves *cold start*. Instead of a replica downloading gigabytes of weights
  sequentially before it can serve a single token, it streams weights in fast, cutting startup from
  minutes to something far shorter. This matters concretely once
  [KEDA autoscaling](Control-Plane-D-Observability.md) is live — a scale-up that takes minutes to become
  useful defeats the point of autoscaling.
- **Harbor** is the separate concern of the *engine image* — signed, scanned, pinned per model version, so
  what actually runs in the container is verifiable and tied to a specific model/version pairing.

These are two parallel inputs into Band 5, not one sequential step.

## Data flow

```
HF mirror → quantisation pipeline → Ceph/SeaweedFS (weights) ─┐
                                                                 ├─→ Band 5 (vLLM replicas)
Harbor (signed engine images) ──────────────────────────────────┘
```

## Related

- [Band 5 — Inference Fleet](Band-5-Inference-Fleet.md) — consumes weights + images from here
- [Band 5's licence table](Band-5-Inference-Fleet.md#-model-weights-are-the-licence-risk-that-actually-matters) — the legal risk this pipeline must track per model
