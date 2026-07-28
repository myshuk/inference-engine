« [Home](Home.md)

# Band 6 — GPU Infrastructure

**Function:** the physical/driver layer everything above runs on.

## Components

### Kubernetes + NVIDIA GPU Operator — the umbrella deployment

Driver/toolkit lifecycle, device plugin, MIG, node labelling. This is what makes GPUs a schedulable
Kubernetes resource at all.

**This is an umbrella, not one component among equals.** NVIDIA's own docs list Node Feature Discovery
(`gpu-operator-node-feature-discovery-master/worker`) and DCGM-exporter (`nvidia-dcgm`,
`nvidia-dcgm-exporter`) as components the GPU Operator itself deploys and manages. Of the four items in
this band's diagram row, this is really the one that brings two of the others along with it — see the
notes on Kueue and DCGM below for what that means in practice.

### NCCL over NVLink / InfiniBand

All-reduce for tensor parallelism — the interconnect that TP replicas depend on for low-latency
cross-GPU communication. This one splits into a library half and an infrastructure half; don't conflate
them.

- **NCCL itself is a library, not a Kubernetes deployment.** It ships inside vLLM's own process — `pip
  install vllm` pulls in PyTorch, which pulls in an `nvidia-nccl-cu*` wheel. When
  [Band 5](Band-5-Inference-Fleet.md) is configured with `tensor_parallel_size > 1`, vLLM automatically
  uses NCCL as PyTorch's `torch.distributed` backend for the all-reduce calls between GPU worker
  processes. There is nothing to stand up separately for this to work on a single node.
- **Single-node NVLink peer access comes for free** from the GPU driver that the GPU Operator (above)
  already installs — no extra component needed.
- ⚠ **Multi-node RDMA/GPUDirect is a genuinely separate deployment: the NVIDIA Network Operator.** It
  installs the MOFED driver, the RDMA shared device plugin, and the GPU peer-memory driver on each node.
  Without it, NCCL doesn't fail — it silently falls back to slow TCP sockets instead of RDMA, which shows
  up as degraded TP performance with no obvious error to point at.

### Kueue — the one standalone deployment in this band

*(kubernetes-sigs, own Helm chart, own `kueue-controller-manager`, own `kueue-system` namespace)*

Gang scheduling — keeps a TP group inside one NVLink domain. Without this, a tensor-parallel replica
could get scheduled across GPUs that don't share fast interconnect, silently destroying the performance
TP was supposed to provide.

**Node Feature Discovery is not a second thing you install alongside Kueue.** It's the labelling
mechanism Kueue's scheduling decisions read from, and — per the GPU Operator note above — it ships
bundled with the GPU Operator already. Kueue is genuinely independent infrastructure; NFD, in this stack,
isn't.

### DCGM + node health — bundled with GPU Operator, not standalone

XID errors, ECC/thermal/power telemetry, auto cordon + drain. Ships as `nvidia-dcgm-exporter`, one of the
components the GPU Operator deploys and manages directly — there's no separate DCGM install to do here.

- ⚠ **Probe generation ability, not liveness.** A CUDA wedge can leave the process reporting healthy
  while it's no longer able to generate tokens. Health checks that only test process liveness will pass
  right through a dead GPU. See [Non-Negotiable Constraints](Non-Negotiable-Constraints.md).

## Deployment count, honestly

Of the four items in this band's diagram row, only **two** are things you actually stand up yourself:
the **GPU Operator** (which brings NFD and DCGM-exporter with it) and **Kueue** (genuinely separate).
**NCCL** is a library with no deployment at all. **Network Operator** (mentioned under NCCL above) is a
third real deployment, but only needed once you have multi-node RDMA to configure.

## Related

- [Band 5 — Inference Fleet](Band-5-Inference-Fleet.md) — what runs on top of this band
- [Non-Negotiable Constraints](Non-Negotiable-Constraints.md) — the generation-ability health probe rule
