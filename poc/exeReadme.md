# Execution Log

Every command actually run for this POC, in order, with a comment on why. Companion to
[`README.md`](README.md) (the plan) — that file says *what* and *why the design is this way*, this file is
*what was actually typed* and *what happened*. Append to this, don't rewrite history in it.

## Phase 0 — Host prerequisites

### Environment check — what's already installed

```bash
which uv; uv --version                 # uv: not installed
brew list --versions podman kind       # podman 5.6.1 already installed (from an earlier, unrelated setup); kind missing
which python3.12; python3.12 --version # /opt/homebrew/bin/python3.12, Python 3.12.11 — native arm64 (Homebrew prefix), not Rosetta
```

Decision: skip `uv`, use plain `venv`/`pip` — the POC's Python dependency set is small (vllm-metal, `openai`
SDK, maybe FastAPI for Phase 2's mocks), so uv's main advantage (fast resolves on large dependency trees)
doesn't pay for itself here. Also skipped `k9s` — it's a UI wrapper over the same calls `kubectl` already
makes, nothing it does is otherwise unavailable.

### Container runtime: Podman

Chose Podman over Docker Desktop / colima: fully open source, no licensing questions, CLI-first. kind's
Podman provider is officially supported but still labeled experimental
(`KIND_EXPERIMENTAL_PROVIDER=podman`) — accepted as a reasonable tradeoff for a single-node local POC.

```bash
podman machine list
# NAME                     VM TYPE     CREATED        LAST UP     CPUS   MEMORY   DISK SIZE
# podman-machine-default*  applehv     10 months ago  Never       6      2GiB     100GiB
```

Found a pre-existing (unused) Podman machine from an earlier, unrelated setup — 2GiB memory would be tight
once Postgres/Valkey/RLS/Kafka/ClickHouse/Prometheus/Grafana/agentgateway/GIE are all running inside it
(vLLM itself runs natively on the host, not in this VM, so it doesn't count against this budget). Bumped
memory before first start rather than resizing later mid-POC:

```bash
podman machine set --memory 8192 podman-machine-default
podman machine list   # confirmed: MEMORY now 8GiB, CPUS/DISK unchanged (6 / 100GiB)
```

Started the machine and verified the CLI can actually talk to it:

```bash
podman machine start podman-machine-default
# Note: rootless mode; no Docker-API-compatible socket at the default path unless
# `podman-mac-helper` is installed. Not needed — kind's podman provider shells out to
# the `podman` CLI directly, it doesn't need the Docker socket path.

podman info --format '{{.Host.Arch}} / {{.Host.OS}} / rootless={{.Host.Security.Rootless}}'
# arm64 / linux / rootless=true

podman ps   # empty table, but responded — confirms the CLI-to-VM connection works
```

### Installing kind

```bash
brew install kind
kind version
# kind v0.32.0 go1.26.3 darwin/arm64
```

### Smoke test: kind + Podman actually work together

Verifying the whole chain end-to-end now, rather than discovering a Podman/kind integration problem later
in Phase 2 once real manifests are riding on it:

```bash
export KIND_EXPERIMENTAL_PROVIDER=podman
kind create cluster --name smoke-test
# ✓ node image, nodes, control-plane, CNI, StorageClass all came up clean

kubectl --context kind-smoke-test get nodes -o wide
# smoke-test-control-plane   Ready   control-plane   v1.36.1   containerd://2.3.1

kind delete cluster --name smoke-test   # throwaway — Phase 2 creates the real cluster
```

**Result:** Podman (rootless, applehv, 8GiB) → kind (experimental Podman provider) → kubectl all confirmed
working before building anything on top of it.

### KIND_EXPERIMENTAL_PROVIDER

```bash
# Added to ~/.zshrc directly (persists for interactive terminal use):
export KIND_EXPERIMENTAL_PROVIDER=podman
```

Note: this tool's Bash shell doesn't source `~/.zshrc`, so it's exported inline in commands here going
forward.

## Phase 1 — Band 5: real vLLM via vllm-metal

```bash
# Save the installer locally instead of piping curl straight into bash
mkdir -p poc/extFiles
curl -fsSL https://raw.githubusercontent.com/vllm-project/vllm-metal/main/install.sh \
  -o poc/extFiles/vllm-metal-install.sh
chmod +x poc/extFiles/vllm-metal-install.sh

# Run it — installs vLLM core + vllm-metal plugin into ~/.venv-vllm-metal
bash poc/extFiles/vllm-metal-install.sh
# Result: vllm==0.26.0+cpu, vllm-metal==0.3.0.dev20260805172839, mlx==0.32.0

# Verify the metal platform plugin is actually picked up (not the CPU fallback)
source ~/.venv-vllm-metal/bin/activate
vllm --version
python3 -c "import vllm_metal; print('vllm_metal import OK')"
# Result: "Platform plugin metal is activated", vllm 0.26.0+cpu, import OK
```

### Serving a model

```bash
# Start vLLM serving Qwen2.5-1.5B-Instruct on :8001, log to a file, run detached
source ~/.venv-vllm-metal/bin/activate
nohup vllm serve Qwen/Qwen2.5-1.5B-Instruct --port 8001 \
  > poc/engine/vllm-8001.log 2>&1 &
# Result: PID 29047. MLX device on GPU (Metal), 17.8GB wired memory limit,
# chunked prefill/paged attention enabled. Server ready after ~4 min (model download + load).

# Smoke test: real request against /v1/chat/completions
curl -s http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen2.5-1.5B-Instruct", "messages": [{"role": "user", "content": "Say hello in exactly 5 words."}], "max_tokens": 30}'
# Result: valid OpenAI-shaped response, usage: {prompt_tokens: 37, completion_tokens: 11, total_tokens: 48}

# Streaming: confirm real SSE framing (data: {...} chunks, [DONE] terminator)
curl -N -s http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen2.5-1.5B-Instruct", "messages": [{"role": "user", "content": "Count from 1 to 5."}], "max_tokens": 40, "stream": true}'
# Result: correct per-token delta chunks, terminated with data: [DONE]

# Streaming usage needs an explicit opt-in flag (OpenAI protocol) — verify vLLM supports it
curl -N -s http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen2.5-1.5B-Instruct", "messages": [{"role": "user", "content": "Count from 1 to 3."}], "max_tokens": 20, "stream": true, "stream_options": {"include_usage": true}}'
# Result: final chunk has choices:[] + usage object, then [DONE] — criterion 1 fully satisfied
```

### Acceptance criterion 2: official OpenAI SDK

```bash
# poc/engine/test_client.py — chat.completions.create() via the openai SDK,
# non-streaming + streaming(include_usage), asserts usage math is consistent
source ~/.venv-vllm-metal/bin/activate
python3 poc/engine/test_client.py
# Result: both calls succeeded, correct usage in both, "All checks passed."
```

## Phase 2 — Bands 2-4: kind + Gateway API + agentgateway + GIE

```bash
# poc/k8s/kind-config.yaml — control-plane + worker (2 nodes, not 1: Phase 4's
# fake-GPU node labelling for Kueue needs a worker node to label)
export KIND_EXPERIMENTAL_PROVIDER=podman
kind create cluster --name inference-poc --config poc/k8s/kind-config.yaml
kubectl --context kind-inference-poc get nodes -o wide
# Result: inference-poc-control-plane and inference-poc-worker, both Ready, v1.36.1
```

### CRDs

```bash
POC=poc
# Gateway API CRDs (kustomize remote base), rendered to a local file first
kubectl kustomize "https://github.com/kubernetes-sigs/gateway-api/config/crd?ref=v1.5.1" \
  > $POC/extFiles/gateway-api-crds-v1.5.1.yaml

# GIE CRDs (InferencePool) — note: EPP/InferenceObjective/InferenceModelRewrite
# have moved to llm-d/llm-d-router; this repo now only hosts InferencePool + LWEPP
curl -fsSL https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/v1.5.0/v1-manifests.yaml \
  -o $POC/extFiles/gie-crds-v1.5.0.yaml

kubectl --context kind-inference-poc apply -f $POC/extFiles/gateway-api-crds-v1.5.1.yaml
kubectl --context kind-inference-poc apply -f $POC/extFiles/gie-crds-v1.5.0.yaml

kubectl --context kind-inference-poc get crds | grep -E "gateway|inference"
# Result: 9 CRDs registered — gatewayclasses, gateways, httproutes, grpcroutes,
# tlsroutes, listenersets, referencegrants, backendtlspolicies, inferencepools
```

### agentgateway

```bash
# poc/k8s/agentgateway-crds-lab/ and poc/k8s/agentgateway-lab/ — umbrella charts,
# each pinning one upstream OCI chart (cr.agentgateway.dev/charts/*) as a dependency
# in Chart.yaml + an explicit values.yaml override (pattern copied from
# ../../explore/ceph/helm/ceph-lab). Not bundled into one umbrella: CRDs must exist
# before the controller starts, and Helm's crds/-folder install-first guarantee only
# applies to a chart's own top-level crds/, not a subchart's.
helm dependency update poc/k8s/agentgateway-crds-lab
helm dependency update poc/k8s/agentgateway-lab

helm upgrade -i --kube-context kind-inference-poc -n agentgateway-system --create-namespace \
  agentgateway-crds poc/k8s/agentgateway-crds-lab
helm upgrade -i --kube-context kind-inference-poc -n agentgateway-system \
  agentgateway poc/k8s/agentgateway-lab
# Result (CRDs): agentgatewaybackends/models/parameters/policies.agentgateway.dev registered
# Result (control plane): pod ErrImagePull — see fix below
```

### Fix: Netskope TLS interception breaks image pulls from inside kind nodes

Root cause: this machine runs Netskope (corporate TLS inspection, cert issuer shows
`O=Publicis Groupe, CN=ca.publicisgroupe.de.goskope.com`). The Mac host trusts its CA; the
Podman VM and the kind node containers (nested one level deeper, separate Debian rootfs) don't —
two separate trust stores needed fixing, not one. The Mac's keychain also held both an old and a
current-generation Netskope CA pair with identical subject/CN but different keys — matching had
to be done by SHA-256 fingerprint against a live handshake, not by name.

The two certs that matched (current as of 2026-08-14) are saved at
[`poc/extFiles/certs/netskope-root-ca.pem`](extFiles/certs/netskope-root-ca.pem) and
[`poc/extFiles/certs/netskope-intermediate-ca.pem`](extFiles/certs/netskope-intermediate-ca.pem).
If Netskope rotates again, re-run the discovery steps below to get a fresh pair.

```bash
# 1. Capture the live cert chain actually being presented right now (run from inside the
#    Podman VM, since that's the network path that matters)
podman machine ssh -- "openssl s_client -connect pkg-containers.githubusercontent.com:443 -showcerts </dev/null" \
  > poc/extFiles/certs/live-chain-capture.txt
grep -B1 -A20 "BEGIN CERTIFICATE" poc/extFiles/certs/live-chain-capture.txt

# 2. Export every "Publicis"/"netskope"-labeled cert from the macOS System keychain and
#    compare SHA-256 fingerprints against step 1's chain to find the CURRENT (not stale) pair
security find-certificate -a -p /Library/Keychains/System.keychain > /tmp/all-certs.pem
csplit -z -f /tmp/kc_cert_ /tmp/all-certs.pem '/-----BEGIN CERTIFICATE-----/' '{*}'
for f in /tmp/kc_cert_*; do openssl x509 -in "$f" -noout -subject -fingerprint -sha256 2>/dev/null; done \
  | grep -B1 -i "publicis\|netskope"
# Match fingerprints against step 1's output; save the matching root + intermediate as
# poc/extFiles/certs/netskope-root-ca.pem and netskope-intermediate-ca.pem

# 3. Install into the Podman VM's own trust store (Fedora-based)
cat poc/extFiles/certs/netskope-root-ca.pem | podman machine ssh -- "sudo tee /tmp/netskope-root.pem > /dev/null"
cat poc/extFiles/certs/netskope-intermediate-ca.pem | podman machine ssh -- "sudo tee /tmp/netskope-intermediate.pem > /dev/null"
podman machine ssh -- "sudo trust anchor /tmp/netskope-root.pem && sudo trust anchor /tmp/netskope-intermediate.pem && sudo update-ca-trust extract"

# 4. Install the SAME two certs into each kind node container separately (Debian-based,
#    a nested container inside the VM with its own rootfs -- fixing the VM alone isn't enough)
for node in inference-poc-control-plane inference-poc-worker; do
  podman cp poc/extFiles/certs/netskope-root-ca.pem "$node:/usr/local/share/ca-certificates/netskope-root.crt"
  podman cp poc/extFiles/certs/netskope-intermediate-ca.pem "$node:/usr/local/share/ca-certificates/netskope-intermediate.crt"
  podman exec "$node" update-ca-certificates
  podman exec "$node" systemctl restart containerd
done

kubectl --context kind-inference-poc delete pod -n agentgateway-system -l app.kubernetes.io/name=agentgateway
kubectl --context kind-inference-poc get pods -n agentgateway-system
# Result: 1/1 Running
kubectl --context kind-inference-poc get gatewayclass
# Result: agentgateway, controller agentgateway.dev/agentgateway, ACCEPTED=True
```

### Pausing / resuming the environment

```bash
# Pause: stop the engine and the whole Podman VM (pauses kind's node containers, doesn't delete them)
pkill -f "vllm serve"      # SIGTERM sometimes not enough -- confirm with ps, kill -9 <pid> if still alive
podman machine stop

# Resume: start the VM back up, then the kind node containers explicitly --
# they don't auto-restart with the VM
podman machine start
podman start inference-poc-control-plane inference-poc-worker
kubectl --context kind-inference-poc get nodes
kubectl --context kind-inference-poc get pods -n agentgateway-system
# Result: nodes Ready, agentgateway pod restarts on its own (RESTARTS count goes up) and
# settles back to 1/1 within ~1min -- all CRDs/GatewayClass state persisted through the pause
```

### llm-d-router EPP overlay

EPP (InferenceObjective/InferenceModelRewrite too) moved from kubernetes-sigs/gateway-api-inference-extension
to llm-d/llm-d-router. That repo's own dev overlays only target Istio/kgateway, not agentgateway --
poc/k8s/llm-d-epp-overlay/ is our own overlay patching their generic base for agentgateway's GatewayClass.

```bash
POC="/Users/malshukl/Documents/build/projects/inference_engine/inference-design/inference-engine/poc"

# Extra CRDs llm-d-router's EPP RBAC needs (llm-d.ai group, not inference.networking.k8s.io) --
# separate from the InferencePool CRD installed earlier
kubectl kustomize "https://github.com/llm-d/llm-d-router/config/crd?ref=v0.10.0" \
  > $POC/extFiles/llm-d-router-crds-v0.10.0.yaml
kubectl --context kind-inference-poc apply -f $POC/extFiles/llm-d-router-crds-v0.10.0.yaml
# Result: inferencemodelrewrites.llm-d.ai + inferenceobjectives.llm-d.ai created,
# inferencepools.inference.networking.k8s.io reconfigured (harmless dup from the same kustomization)

kubectl --context kind-inference-poc create namespace inference-poc

# poc/k8s/llm-d-epp-overlay/ — kustomization.yaml (remote base:
# deploy/components/inference-gateway pinned @v0.10.0) + patch-gateways.yaml
# (sets gatewayClassName: agentgateway) + epp-configmap.yaml (wraps llm-d-router's
# plain deploy/config/epp-config.yaml as the ConfigMap the EPP mounts) + env.sh
# (values for the base's ${VAR} placeholders -- Kustomize doesn't resolve these,
# needs envsubst same as llm-d-router's own scripts/kind-dev-env.sh does it)
source $POC/k8s/llm-d-epp-overlay/env.sh
kubectl kustomize $POC/k8s/llm-d-epp-overlay | envsubst | \
  kubectl --context kind-inference-poc apply -f -
# Result: serviceaccount, 2 roles + bindings, epp-config configmap, service, deployment,
# gateway, httproute, inferencepool (vllm-mock-pool) all created

kubectl --context kind-inference-poc get pods -n inference-poc
# Result: inference-gateway-... (agentgateway's provisioned data-plane proxy) 1/1 Running,
# vllm-epp-... 1/1 Running
kubectl --context kind-inference-poc get gateway -n inference-poc
# Result: inference-gateway, CLASS=agentgateway, PROGRAMMED=True
```

EPP logs confirm it's watching `InferencePool vllm-mock-pool` (selector `app=vllm-mock-pool`) and has
its `ext-proc` gRPC server up on :9002 — zero endpoints until the mock servers exist (next step).

⚠ Config note: `epp-configmap.yaml` uses llm-d-router's current plugin set (`prefix-cache-scorer`,
`decode-filter`, `max-score-picker`) — different from `QueueScorer`/`KVCacheUtilizationScorer` in
`wiki/Band-4-Inference-Routing.md`, which documents the original (pre-split) GIE EPP. Wiki update
pending, not done yet.

### Resuming after the pause, and adding queue/kv-cache scoring

```bash
podman machine start
podman start inference-poc-control-plane inference-poc-worker
kubectl --context kind-inference-poc -n agentgateway-system delete pod -l app.kubernetes.io/name=agentgateway
# Result: transient BackendNotFound/timeout right after restart -- one-off, cleared on its own
```

`prefix-cache-scorer`/`decode-filter` don't read any load metric at all (confirmed against llm-d-router's
`docs/plugin-metric-protocol.md` and its `soft-reflective-ceiling-epp-config.yaml` sample) — added
`queue-scorer` + `kv-cache-utilization-scorer` so the mocks have something controllable to prove EPP
selection against. Real metric name is `vllm:kv_cache_usage_perc` (the wiki never actually specified a
metric name; an earlier note here wrongly claimed it said `vllm:gpu_cache_usage_perc` — corrected, and
the wiki now has the real name).

```bash
# poc/k8s/llm-d-epp-overlay/epp-configmap.yaml -- added queue-scorer (weight 2) and
# kv-cache-utilization-scorer (weight 2) alongside prefix-cache-scorer (weight 1)
kubectl --context kind-inference-poc -n inference-poc apply -f poc/k8s/llm-d-epp-overlay/epp-configmap.yaml
kubectl --context kind-inference-poc -n inference-poc rollout restart deployment/vllm-epp
```

### Mock servers (criterion 3)

`poc/k8s/mock-servers/mock_server.py` — stdlib-only Python (`http.server`), no custom image build needed.
Serves `/health`, `/metrics` (`vllm:num_requests_waiting`, `vllm:kv_cache_usage_perc`), `/control`
(GET/POST to read/set those live), `/v1/chat/completions` (streaming + non-streaming, content identifies
the answering pod).

```bash
cd poc/k8s/mock-servers
kubectl create configmap vllm-mock-server-code --from-file=mock_server.py --dry-run=client -o yaml \
  > configmap.yaml
# vllm-mock-a: INIT_NUM_REQUESTS_WAITING=0, INIT_KV_CACHE_USAGE_PERC=0.05 (low load)
# vllm-mock-b: INIT_NUM_REQUESTS_WAITING=50, INIT_KV_CACHE_USAGE_PERC=0.9 (high load)
# both: app=vllm-mock-pool (matches InferencePool selector), containerPort 8000
kubectl --context kind-inference-poc -n inference-poc apply -f configmap.yaml -f deployment.yaml
kubectl --context kind-inference-poc -n inference-poc get pods -l app=vllm-mock-pool
# Result: both 1/1 Running
```

Verified each mock directly (port-forward, bypassing the gateway) before testing through it:

```bash
kubectl --context kind-inference-poc -n inference-poc port-forward deploy/vllm-mock-a 18000:8000 &
curl -s http://localhost:18000/metrics                 # vllm:num_requests_waiting 0.0, vllm:kv_cache_usage_perc 0.05
curl -s http://localhost:18000/v1/chat/completions ...  # valid OpenAI-shaped response
curl -s -X POST http://localhost:18000/control -d '{"num_requests_waiting": 5}'  # confirms live control works
```

### Debugging: agentgateway can't route to the InferencePool at all

Sent a request through the gateway (`svc/inference-gateway` port-forward) expecting EPP to pick between
the two mocks — got `500 backend does not exist` instead. This turned into two real, separate bugs, not
config mistakes on our side:

```bash
# Confirmed the InferencePool object itself is fine and has real endpoints behind it
kubectl --context kind-inference-poc -n inference-poc get inferencepool vllm-mock-pool -o yaml
kubectl --context kind-inference-poc -n inference-poc get httproute vllm-mock-pool-inference-route \
  -o jsonpath='{.status.parents[0].conditions}'
# Result: ResolvedRefs=False, BackendNotFound, "backendRef inference-poc/vllm-mock-pool not found"

# Traced the error in agentgateway's own source (reference_indexes.go, DefaultRouteBackend) to a
# krt.FetchOne on an `agw.InferencePools` collection keyed "namespace/name"
kubectl --context kind-inference-poc get clusterrole agentgateway-agentgateway-system -o yaml
# Result: no rule at all for inference.networking.k8s.io/inferencepools -- looked like the root cause
```

Added a supplemental ClusterRole (`poc/k8s/agentgateway-lab/rbac-inferencepool-patch.yaml`, kept separate
from the Helm-managed one so `helm upgrade` doesn't revert it) granting get/list/watch on `inferencepools`
to the `agentgateway` ServiceAccount. Restarted — same error, unchanged. Verified the grant actually took:

```bash
kubectl --context kind-inference-poc auth can-i list inferencepools.inference.networking.k8s.io \
  --as=system:serviceaccount:agentgateway-system:agentgateway   # yes
kubectl --context kind-inference-poc auth can-i watch inferencepools.inference.networking.k8s.io \
  --as=system:serviceaccount:agentgateway-system:agentgateway   # yes
```

RBAC wasn't it. Built the criterion-4 wiring next since it uses a different resolution code path
(`agw.Backends`, not `agw.InferencePools`) and might sidestep whatever this was:

```bash
# poc/k8s/mock-servers/agentgateway-backend.yaml -- AgentgatewayBackend, spec.ai.provider.custom.backendRef
# targets {group: inference.networking.k8s.io, kind: InferencePool, name: vllm-mock-pool}
kubectl --context kind-inference-poc -n inference-poc apply -f poc/k8s/mock-servers/agentgateway-backend.yaml

# poc/k8s/llm-d-epp-overlay/patch-httproute-backend.yaml -- repoints the HTTPRoute's backendRef from the
# InferencePool directly to this AgentgatewayBackend; added to kustomization.yaml's patches list
source poc/k8s/llm-d-epp-overlay/env.sh
kubectl kustomize poc/k8s/llm-d-epp-overlay | envsubst | kubectl --context kind-inference-poc apply -f -
kubectl --context kind-inference-poc -n inference-poc get agentgatewaybackend vllm-mock-pool-backend \
  -o jsonpath='{.status.conditions}'
# Result: same underlying error, one layer deeper: "failed to translate LLM provider:
# backendRef inference-poc/vllm-mock-pool not found"
```

Checked agentgateway's GitHub issues — found a merged fix (closing #2976, merged 2026-08-12) for
`AgentgatewayBackend` custom-provider + `InferencePool`, but our pin (v1.4.1) released 2026-07-29, before
it. Bumped to v1.5.0-beta.1 (only release with it; no stable release has it yet):

```bash
# poc/k8s/agentgateway-crds-lab/Chart.yaml and poc/k8s/agentgateway-lab/Chart.yaml:
# dependency version v1.4.1 -> v1.5.0-beta.1 (deliberate, temporary -- revisit once a stable release ships)
cd poc/k8s/agentgateway-crds-lab && helm dependency update
helm --kube-context kind-inference-poc upgrade agentgateway-crds . -n agentgateway-system
cd ../agentgateway-lab && helm dependency update
helm --kube-context kind-inference-poc upgrade agentgateway . -n agentgateway-system
# Result: same "backendRef ... not found" error, unchanged -- #2976 fixed a different bug
# (EPP silently skipped), not this one
```

Read agentgateway's own `collection.go` directly and found the actual root cause -- not a bug, a feature
flag defaulted off:

```go
// controller/pkg/agentgateway/plugins/collection.go:209
InferencePools: krt.NewStaticCollection[*inf.InferencePool](nil, nil, ...)  // permanently empty by default
if settings.EnableInferExt { agwCollections.InferencePools = krt.WrapClient(...) }  // only real if this is true
```

Traced `EnableInferExt` to Helm value `inferenceExtension.enabled` (env var `AGW_ENABLE_INFER_EXT`, chart
`templates/deployment.yaml`) — never set in our `values.yaml`. Set it:

```yaml
# poc/k8s/agentgateway-lab/values.yaml
agentgateway:
  inferenceExtension:
    enabled: true
```

```bash
cd poc/k8s/agentgateway-lab
helm --kube-context kind-inference-poc upgrade agentgateway . -n agentgateway-system
kubectl --context kind-inference-poc -n inference-poc get agentgatewaybackend vllm-mock-pool-backend \
  -o jsonpath='{.status.conditions}'
# Result: Accepted=True, "Backend successfully accepted"
```

### Criterion 3 verified end-to-end

```bash
kubectl --context kind-inference-poc -n inference-poc port-forward svc/inference-gateway 18080:80 &
curl -s -X POST http://localhost:18080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"mock","messages":[{"role":"user","content":"hi"}]}'
# Result: 200, "mock response from vllm-mock-a-..." -- 5/5 repeats, all mock-a (the low-load pod)

# Flip the load via /control and confirm EPP's pick flips with it
curl -s -X POST http://localhost:18000/control -d '{"num_requests_waiting": 50, "kv_cache_usage_perc": 0.9}'
curl -s -X POST http://localhost:18001/control -d '{"num_requests_waiting": 0, "kv_cache_usage_perc": 0.05}'
# ... same 5 requests through the gateway again
# Result: 5/5 now "mock response from vllm-mock-b-..." -- selection tracks the metrics, not sticky/random

# Reset both mocks back to their declared deployment.yaml baseline afterward
curl -s -X POST http://localhost:18000/control -d '{"num_requests_waiting": 0, "kv_cache_usage_perc": 0.05}'
curl -s -X POST http://localhost:18001/control -d '{"num_requests_waiting": 50, "kv_cache_usage_perc": 0.9}'
```

### Resuming, and the agentgateway admin UI

```bash
podman machine start
podman start inference-poc-control-plane inference-poc-worker
kubectl --context kind-inference-poc get pods -A   # all settle back to Running within ~30s
```

The data-plane proxy pod (not the control-plane controller) runs an admin UI + stats endpoint:

```bash
kubectl --context kind-inference-poc -n inference-poc port-forward deploy/inference-gateway 15000:15000 &
curl -sI http://localhost:15000/ui   # 200, real HTML/JS admin UI
kubectl --context kind-inference-poc -n inference-poc port-forward deploy/inference-gateway 15020:15020 &
curl -s http://localhost:15020/metrics | grep -iE "request|token"   # Prometheus text
```

### Criterion 4 verified: token-accounting regression test

Same `HTTPRoute` object, swapped `backendRefs` between the two configurations via `kubectl patch`
(no file changes -- reverted after), comparing agentgateway's own `agentgateway_gen_ai_client_token_usage`
metric (not the client-visible JSON `usage`, which comes from the mock either way) before/after each request.

```bash
# Baseline (still on AgentgatewayBackend from earlier): count=1, sum=4.0(output)/1.0(input)
curl -s http://localhost:15020/metrics | grep agentgateway_gen_ai_client_token_usage

# (a) naive: patch backendRefs to InferencePool directly
kubectl --context kind-inference-poc -n inference-poc patch httproute vllm-mock-pool-inference-route \
  --type=json -p '[{"op":"replace","path":"/spec/rules/0/backendRefs","value":[
    {"group":"inference.networking.k8s.io","kind":"InferencePool","name":"vllm-mock-pool","port":8000}]}]'
# Result: ResolvedRefs=True -- routes fine now (inferenceExtension.enabled fix applies to this path too)

curl -s -X POST http://localhost:18080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"mock","messages":[{"role":"user","content":"hi"}]}'
# Result: 200, correct usage in the raw JSON body (prompt_tokens:1, completion_tokens:4) --
# response "model" field came back as "mock-model" (mock's own default) instead of the requested
# "mock" -- naive path may mutate/drop the model field somewhere; not investigated further.

curl -s http://localhost:15020/metrics | grep agentgateway_gen_ai_client_token_usage
# Result: count=1, sum=4.0/1.0 -- UNCHANGED. agentgateway never parsed the body, nothing metered.

# (b) corrected: patch backendRefs back to AgentgatewayBackend
kubectl --context kind-inference-poc -n inference-poc patch httproute vllm-mock-pool-inference-route \
  --type=json -p '[{"op":"replace","path":"/spec/rules/0/backendRefs","value":[
    {"group":"agentgateway.dev","kind":"AgentgatewayBackend","name":"vllm-mock-pool-backend","weight":1}]}]'

curl -s -X POST http://localhost:18080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"mock","messages":[{"role":"user","content":"hi again"}]}'
# Result: 200, usage prompt_tokens:2, completion_tokens:4

curl -s http://localhost:15020/metrics | grep agentgateway_gen_ai_client_token_usage
# Result: count=2 (was 1), sum=8.0/3.0 (was 4.0/1.0) -- incremented by exactly this response's
# real token counts. Criterion 4 proven: (b) meters, (a) silently doesn't, same route object both times.
```

## Phase 2.5 — Control Plane B: human signup via Keycloak (no SSO)

```bash
helm repo add codecentric https://codecentric.github.io/helm-charts
kubectl --context kind-inference-poc create namespace keycloak

# poc/k8s/keycloak-lab/ -- Chart.yaml pins codecentric/keycloakx v7.3.1 (official
# quay.io/keycloak/keycloak image; bitnami/keycloak gates most tags behind a paid tier).
# values.yaml: args=[start-dev] (in-memory H2), fixed bootstrap admin creds (POC only)
cd poc/k8s/keycloak-lab && helm dependency update
helm --kube-context kind-inference-poc install keycloak . -n keycloak
kubectl --context kind-inference-poc -n keycloak wait --for=condition=Ready pod/keycloak-keycloakx-0 --timeout=180s
# Result: 1/1 Running

kubectl --context kind-inference-poc -n keycloak port-forward svc/keycloak-keycloakx-http 8180:80 &
```

### Create the developers realm with self-registration enabled

```bash
TOKEN=$(curl -s -X POST http://localhost:8180/auth/realms/master/protocol/openid-connect/token \
  -d "client_id=admin-cli" -d "username=admin" -d "password=admin-poc-only" -d "grant_type=password" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

curl -s -X POST http://localhost:8180/auth/admin/realms -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{
    "realm": "developers", "enabled": true, "registrationAllowed": true,
    "registrationEmailAsUsername": true, "verifyEmail": false,
    "resetPasswordAllowed": true, "loginWithEmailAllowed": true
  }'
# Result: 201. Not "master" -- that's reserved for Keycloak's own admin.
```

### Prove human signup via the actual public registration form (not the admin API)

Admin-created users skip CSRF and the real signup code path -- proves the API works, not that a human's
browser flow does. `account-console` enforces PKCE, so a code_verifier/S256 challenge is required even for
this scripted version of the flow.

```bash
JAR=/tmp/kc_cookies.txt
VERIFIER=$(python3 -c "import secrets; print(secrets.token_urlsafe(64)[:64])")
CHALLENGE=$(python3 -c "
import hashlib, base64
d = hashlib.sha256('$VERIFIER'.encode()).digest()
print(base64.urlsafe_b64encode(d).decode().rstrip('='))")

LOGIN_PAGE=$(curl -s -c "$JAR" -G "http://localhost:8180/auth/realms/developers/protocol/openid-connect/auth" \
  --data-urlencode "client_id=account-console" \
  --data-urlencode "redirect_uri=http://localhost:8180/auth/realms/developers/account/" \
  --data-urlencode "response_type=code" --data-urlencode "scope=openid" \
  --data-urlencode "code_challenge=$CHALLENGE" --data-urlencode "code_challenge_method=S256")
# Result: without code_challenge, this 302s with error=invalid_request -- account-console requires PKCE

# Extract the registration link (login-actions/registration, not .../registrations -- easy typo)
REG_LINK=$(echo "$LOGIN_PAGE" | grep -o 'href="[^"]*login-actions/registration[^"]*"' | head -1 \
  | sed 's/href="//;s/"$//;s/&amp;/\&/g')
REG_PAGE=$(curl -s -c "$JAR" -b "$JAR" "http://localhost:8180$REG_LINK")

# Extract the form's action URL (carries session_code + execution, both single-use)
FORM_ACTION=$(echo "$REG_PAGE" | grep -o '<form[^>]*action="[^"]*"' | head -1 \
  | grep -o 'action="[^"]*"' | sed 's/action="//;s/"$//;s/&amp;/\&/g')

curl -sv -c "$JAR" -b "$JAR" -X POST "$FORM_ACTION" \
  --data-urlencode "firstName=Test" --data-urlencode "lastName=Developer" \
  --data-urlencode "email=test.developer@example.com" \
  --data-urlencode "password=TestPassw0rd!23" --data-urlencode "password-confirm=TestPassw0rd!23"
# Result: 302, Location carries a real authorization code -- signup + immediate auth succeeded

curl -s http://localhost:8180/auth/admin/realms/developers/users -H "Authorization: Bearer $TOKEN"
# Result: test.developer@example.com present, enabled=true, requiredActions=[]
```

⚠ **Confirmed the hard way:** paused/resumed the environment between building this and testing it --
dev-mode's H2 database is genuinely ephemeral, the pod restart wiped the `developers` realm entirely, had
to recreate it from scratch before the registration test could run.

## Phase 3 — Band 3: identity, tenancy, counters

```bash
kubectl --context kind-inference-poc create namespace identity-tenancy
```

### Postgres

`poc/k8s/postgres/schema.sql` -- orgs -> projects -> plans -> hashed api_keys -> model_allowlist, per the
wiki. ConfigMap mounted at `/docker-entrypoint-initdb.d` so the official postgres image auto-runs it on
first init. Single Deployment + PVC (not CloudNativePG) -- the plan's own simpler option.

```bash
cd poc/k8s/postgres
kubectl create configmap postgres-schema --from-file=schema.sql --dry-run=client -o yaml \
  > postgres-schema-configmap.yaml
kubectl --context kind-inference-poc -n identity-tenancy apply -f postgres-schema-configmap.yaml -f postgres.yaml
kubectl --context kind-inference-poc -n identity-tenancy wait --for=condition=Ready pod -l app=postgres --timeout=90s

POD=$(kubectl --context kind-inference-poc -n identity-tenancy get pods -l app=postgres -o jsonpath='{.items[0].metadata.name}')
kubectl --context kind-inference-poc -n identity-tenancy exec "$POD" -- psql -U identity_tenancy -d identity_tenancy -c "
SELECT o.name AS org, pr.name AS project, pl.name AS plan, ak.key_prefix, ma.model_name
FROM orgs o JOIN projects pr ON pr.org_id = o.id JOIN plans pl ON pl.id = pr.plan_id
JOIN api_keys ak ON ak.project_id = pr.id JOIN model_allowlist ma ON ma.project_id = pr.id;"
# Result: acme-poc | acme-poc-default | free | sk-poc-test | mock -- seed data joins correctly
```

Postgres user/db were initially named "band3" (leftover from an early folder-naming choice, corrected to
`poc/k8s/postgres/` per user feedback -- component name, not phase number). Renamed to `identity_tenancy`
for consistency:

```bash
# POSTGRES_USER/POSTGRES_DB only apply on first init of an empty data dir -- rename requires
# a fresh PVC, not just editing the Secret. Fine, it's only seed data.
kubectl --context kind-inference-poc -n identity-tenancy delete deployment postgres
kubectl --context kind-inference-poc -n identity-tenancy delete pvc postgres-data
kubectl --context kind-inference-poc -n identity-tenancy apply -f postgres.yaml
# Result: re-verified same query above with -U identity_tenancy -d identity_tenancy -- same output
```

### Valkey

```bash
kubectl --context kind-inference-poc -n identity-tenancy apply -f poc/k8s/valkey/valkey.yaml
kubectl --context kind-inference-poc -n identity-tenancy wait --for=condition=Ready pod -l app=valkey --timeout=60s

POD=$(kubectl --context kind-inference-poc -n identity-tenancy get pods -l app=valkey -o jsonpath='{.items[0].metadata.name}')
kubectl --context kind-inference-poc -n identity-tenancy exec "$POD" -- valkey-cli set test-key "hello"
kubectl --context kind-inference-poc -n identity-tenancy exec "$POD" -- valkey-cli get test-key
# Result: OK / hello
```

### envoyproxy/ratelimit (RLS)

Docker Hub for this image hasn't been pushed to since March 2021 (confirmed via their own
.github/workflows/{main,release}.yaml -- Docker Hub only, no GHCR, no new v* tag since 2020). Pinned the
last real push:

```bash
gh api repos/envoyproxy/ratelimit/releases --jq '.[0:5] | .[] | .tag_name'   # newest is v1.4.0, from 2020
curl -s "https://hub.docker.com/v2/repositories/envoyproxy/ratelimit/tags?page_size=25&ordering=-last_updated" \
  | python3 -c "import json,sys; [print(r['name'],r['last_updated']) for r in json.load(sys.stdin)['results']]"
# Result: newest real tag is c03723f3, 2021-03-30 -- pinned that

cd poc/k8s/rls
kubectl create configmap rls-config --from-file=config.yaml --dry-run=client -o yaml > rls-config-configmap.yaml
kubectl --context kind-inference-poc -n identity-tenancy apply -f rls-config-configmap.yaml -f rls.yaml
kubectl --context kind-inference-poc -n identity-tenancy logs -l app=rls | grep -iE "load|domain|listening"
# Result: loading domain: inference-poc, both descriptors loaded (api_key_rpm=60/min, api_key_tpm=10000/min),
# listening on :8081 (gRPC) :8080 (HTTP) :6070 (debug), config_load_success:1
```

### Verifying RLS's ShouldRateLimit RPC directly (not yet wired to agentgateway)

```bash
brew install grpcurl
kubectl --context kind-inference-poc -n identity-tenancy port-forward svc/rls 8081:8081 &
grpcurl -plaintext localhost:8081 list
# Result: "server does not support the reflection API" -- need the actual .proto

# Real proto pulls in Envoy's validate/udpa annotation deps, unnecessary for wire testing.
# Wrote /tmp/rls-protos/rls-minimal.proto: same service/messages, same field numbers, no deps.
# (Confirmed this build's version first: src/server/server.go at commit c03723f3... imports
# github.com/envoyproxy/go-control-plane/envoy/service/ratelimit/v3, not v2.)

# RPM check-and-increment
grpcurl -plaintext -import-path /tmp/rls-protos -proto rls-minimal.proto \
  -d '{"domain":"inference-poc","descriptors":[{"entries":[{"key":"api_key_rpm","value":"sk-poc-test"}]}],"hits_addend":1}' \
  localhost:8081 envoy.service.ratelimit.v3.RateLimitService/ShouldRateLimit
# Result: overallCode=OK, limitRemaining=59 (60-1)

# TPM check-and-increment with a variable cost -- the "estimate" half of D11
grpcurl -plaintext -import-path /tmp/rls-protos -proto rls-minimal.proto \
  -d '{"domain":"inference-poc","descriptors":[{"entries":[{"key":"api_key_tpm","value":"sk-poc-test"}]}],"hits_addend":500}' \
  localhost:8081 envoy.service.ratelimit.v3.RateLimitService/ShouldRateLimit
# Result: overallCode=OK, limitRemaining=9500 (10000-500)

# D11's "amend can't refund" claim -- try to give back the unused 450
grpcurl -plaintext -import-path /tmp/rls-protos -proto rls-minimal.proto \
  -d '{"domain":"inference-poc","descriptors":[{"entries":[{"key":"api_key_tpm","value":"sk-poc-test"}]}],"hits_addend":-450}' \
  localhost:8081 envoy.service.ratelimit.v3.RateLimitService/ShouldRateLimit
# Result: client-side error, "invalid syntax" -- hits_addend is uint32, can't even encode a negative.
# Combined with RateLimitService having exactly one RPC (no Amend/Refund method exists at all),
# there's no refund path structurally, not just by convention -- matches D11 exactly.

# Bonus finding: a "zero-cost peek" isn't actually zero-cost
grpcurl -plaintext -import-path /tmp/rls-protos -proto rls-minimal.proto \
  -d '{"domain":"inference-poc","descriptors":[{"entries":[{"key":"api_key_tpm","value":"sk-poc-test"}]}],"hits_addend":0}' \
  localhost:8081 envoy.service.ratelimit.v3.RateLimitService/ShouldRateLimit
# Result: limitRemaining=9999, not 9500 or 10000 -- two things happened: (1) proto3 JSON can't
# distinguish an explicit 0 from "unset", and RLS's documented fallback for unset is +1, not +0;
# (2) enough real time passed since the 500-hit call that the 1-minute fixed window had already
# reset the counter back toward 10000 before this call's implicit +1 -- a live example of the
# wiki's "overshoot is bounded only by window refill" language.
```

## Automated test suite (poc/tests/)

Consolidated all the manual curl/grpcurl verification above into a rerunnable pytest suite -- one file
per phase/component, rerun after every new component to catch regressions, not just test the newest piece.

```bash
mkdir -p poc/tests/protos
cp /tmp/rls-protos/rls-minimal.proto poc/tests/protos/   # move out of /tmp -- permanent asset now

cd poc/tests
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # pytest, requests
pytest -v
```

Three real bugs found writing the tests (not pre-existing bugs in the components -- bugs in the first
draft of the tests themselves):

```bash
# 1. test_03_epp_routing: first request after a /control flip still returned the old pick.
#    EPP re-exports its own Prometheus metrics every ~5s -- added a 10s settle delay after
#    any /control change before asserting on routing (asked user 10 vs 20s -- picked 10,
#    comfortable margin without slowing down a suite meant for frequent re-runs).

# 2. test_04_agentgateway_routing: asserted token counts against the metric's _count series
#    (number of observations, always +1 per request) instead of _sum (actual token totals) --
#    looked like "off by the exact wrong amount" every time. Fixed to read both series correctly.

# 3. test_05_keycloak: registration page came back 400 "Restart login cookie not found" via
#    requests.Session(), even though the identical curl-based flow worked earlier. Debugged with:
python3 -c "
import requests
session = requests.Session()
r = session.get('http://localhost:8180/auth/realms/developers/protocol/openid-connect/auth', params={...})
for c in session.cookies:
    print(c.name, 'secure:', c.secure)
"
# Result: KC_RESTART / AUTH_SESSION_ID are marked Secure. requests correctly (per RFC 6265)
# refuses to resend Secure cookies over plain http:// -- curl apparently didn't enforce this as
# strictly in the earlier manual test. Fixed by attaching cookies explicitly via a Cookie header
# instead of relying on Session's automatic jar.
```

Result: 22 passed, 4 skipped (test_01_vllm -- native vLLM wasn't running at the time), 0 failed.

Added `pytest_sessionstart`/`pytest_sessionfinish` hooks (conftest.py) calling
`helpers.kill_stray_port_forwards()` -- kills any leftover
`kubectl --context kind-inference-poc ... port-forward` processes before and after every run, rather than
relying on manually remembering to `pkill` after ad-hoc debugging. Scoped to this project's context only
(pgrep pattern includes `--context kind-inference-poc`), not a blanket kill of all port-forwards.

```bash
pytest -v   # rerun after adding the hooks
ps aux | grep port-forward | grep -v grep   # confirm nothing left running
# Result: 22 passed, 4 skipped, no stray processes after
```

## Phase 3 step 5 — wiring agentgateway's real traffic-policy stage to call RLS

Checked agentgateway's AgentgatewayPolicy CRD schema (Traffic.RateLimit.Global) against the saved policy
types file from earlier research -- confirmed descriptors/entries/unit/cost fields map directly onto
what RLS already has configured.

```bash
# poc/k8s/rls/referencegrant.yaml -- cross-namespace: policy lives in inference-poc, RLS in identity-tenancy
# poc/k8s/rls/agentgateway-policy.yaml -- AgentgatewayPolicy targeting the HTTPRoute, two descriptors:
#   api_key_rpm (unit: Requests, cost default 1, pre-dispatch)
#   api_key_tpm (unit: Tokens, cost default = real total, post-completion)
# CEL expression request.headers["x-api-key"] for both -- no real auth wired yet, header-based for now
kubectl --context kind-inference-poc apply -f poc/k8s/rls/referencegrant.yaml
kubectl --context kind-inference-poc apply -f poc/k8s/rls/agentgateway-policy.yaml
kubectl --context kind-inference-poc -n inference-poc get agentgatewaypolicy vllm-mock-pool-rate-limit -o yaml
# Result: Accepted=True, Attached=True
```

### First test showed a flat cost of 1 regardless of real usage -- investigated at length

```bash
kubectl --context kind-inference-poc -n inference-poc port-forward svc/inference-gateway 18080:80 &
POD=$(kubectl --context kind-inference-poc -n identity-tenancy get pods -l app=rls -o jsonpath='{.items[0].metadata.name}')
kubectl --context kind-inference-poc -n identity-tenancy logs "$POD" -f --since=1s > /tmp/rls-live.log &
curl -s -X POST http://localhost:18080/v1/chat/completions -H 'Content-Type: application/json' \
  -H 'x-api-key: sk-poc-test-key-do-not-use-in-prod' \
  -d '{"model":"mock","messages":[{"role":"user","content":"rate limit wiring test"}],"max_tokens":42}'
# Result: usage.total_tokens=8, but RLS log showed api_key_tpm called TWICE, both hits_addend=1 --
# repeated with total_tokens=25 (much longer message) and 2 messages instead of 1 -- delta stayed
# exactly 1 every time, ruling out "counts messages" or "counts something proportional" theories
```

Traced agentgateway's actual Rust source (not just the Go CRD doc comments) to find where this should be
computed:

```bash
gh api repos/agentgateway/agentgateway/contents/crates/agentgateway/src/http/remoteratelimit.rs?ref=v1.5.0-beta.1
gh api repos/agentgateway/agentgateway/contents/crates/agentgateway/src/llm/mod.rs?ref=v1.5.0-beta.1
gh api repos/agentgateway/agentgateway/contents/crates/llm/src/types/completions.rs?ref=v1.5.0-beta.1
```

Found: Tokens-unit really does make two calls (pre-dispatch "estimate" cost=0 without a tokenizer
configured, post-completion "amend" sending a computed delta) -- more sophisticated than the Go CRD doc
comment implied, and closer to D11's real intent. But `to_llm_response()` in types/completions.rs reads
`self.usage.map(|u| u.completion_tokens)` correctly for our exact JSON shape -- the code looked right at
every layer checked. Confirmed `self.usage` wasn't None indirectly: the client's response is the
*re-serialized* parsed object (not raw passthrough), and it correctly echoed real usage numbers back --
so parsing wasn't silently failing.

### Getting live trace logs from the actual data plane to settle it

Patching the managed Deployment's env directly gets reverted by the controller within seconds (confirmed:
patched RUST_LOG, requeried immediately, value was back to "info"). Found the supported mechanism:
`AgentgatewayParameters` attached via the `GatewayClass`'s `parametersRef` -- not continuously reconciled
the same way, so it sticks.

```bash
# poc/k8s/rls/agentgateway-debug-params.yaml (temporary) -- spec.logging.level: "agentgateway=trace"
kubectl --context kind-inference-poc apply -f poc/k8s/rls/agentgateway-debug-params.yaml
kubectl --context kind-inference-poc patch gatewayclass agentgateway --type=merge -p '{"spec":{"parametersRef":{"group":"agentgateway.dev","kind":"AgentgatewayParameters","name":"debug-params","namespace":"inference-poc"}}}'
# Result: new data-plane pod (env change forces a rollout), RUST_LOG=agentgateway=trace confirmed set and NOT reverted

kubectl --context kind-inference-poc -n inference-poc logs <new-pod> -f --since=1s > /tmp/agw-trace.log &
curl ... # same test request as above
grep "hits_addend" /tmp/agw-trace.log
# Result: hits_addend=Some(9) for a request with total_tokens=9 -- CORRECT, matches real usage exactly.
# Repeated with total_tokens=14 -- hits_addend=Some(14). Also correct.
```

**Root cause: not a code or config bug.** The *original* data-plane pod (running 8+ days through dozens
of config changes this session) had accumulated some stale internal state -- a fresh pod (forced by the
AgentgatewayParameters env change) computed real costs correctly immediately, with zero other changes.
Removed the debug logging afterward (trace-level isn't left running normally) and re-confirmed correct
behavior survives without it:

```bash
kubectl --context kind-inference-poc patch gatewayclass agentgateway --type=json -p '[{"op":"remove","path":"/spec/parametersRef"}]'
kubectl --context kind-inference-poc delete -f poc/k8s/rls/agentgateway-debug-params.yaml
rm poc/k8s/rls/agentgateway-debug-params.yaml   # temporary file, not kept
# Result: new pod again (normal logging), confirmed real-cost accounting still correct
```

### Captured as an automated test, not just manual curl+log inspection

```bash
# poc/tests/test_09_rate_limit_wiring.py -- fresh random x-api-key per test (clean RLS descriptor,
# no interference from other runs), sends one real request, confirms via a direct RLS query that
# the counter depleted by exactly the response's real total_tokens. Polls briefly rather than
# assuming the amend call (fire-and-forget async, spawned not awaited) has already landed.
pytest -v
# Result: 24 passed, 4 skipped (vLLM not running) -- test_09's two new tests both pass
```

## Phase 4 — Band 6: GPU infrastructure (simulated)

```bash
# poc/k8s/kueue-lab/Chart.yaml -- dependency kueue v0.19.4, oci://registry.k8s.io/kueue/charts.
# Confirmed both 0.19.3 and 0.19.4 actually pullable (unlike RLS, no staleness here).
helm show chart oci://registry.k8s.io/kueue/charts/kueue --version 0.19.4

cd poc/k8s/kueue-lab
helm dependency update
kubectl --context kind-inference-poc create namespace kueue-system
helm --kube-context kind-inference-poc install kueue . -n kueue-system
kubectl --context kind-inference-poc -n kueue-system get pods
# Result: 1/1 Running

kubectl --context kind-inference-poc get crd | grep kueue
kubectl --context kind-inference-poc -n kueue-system logs deploy/kueue-controller-manager --tail=30
# Result: 11 CRDs installed, internal cert management working ("CA certs are injected to webhooks"),
# no cert-manager needed, all reconcilers started cleanly
```

### Fake GPU resource

```bash
kubectl --context kind-inference-poc patch node inference-poc-worker --subresource=status --type='json' -p='[
  {"op": "add", "path": "/status/capacity/nvidia.com~1gpu", "value": "2"},
  {"op": "add", "path": "/status/allocatable/nvidia.com~1gpu", "value": "2"}
]'
kubectl --context kind-inference-poc get node inference-poc-worker \
  -o jsonpath='{.status.capacity.nvidia\.com/gpu}{"\n"}{.status.allocatable.nvidia\.com/gpu}{"\n"}'
# Result: 2 / 2 -- JSON Pointer escaping: "/" in "nvidia.com/gpu" -> "~1" per RFC 6902
```

`poc/k8s/kueue-lab/fake-gpu-queue.yaml` -- ResourceFlavor (no node labels needed, single-worker-node POC)
+ ClusterQueue (nominalQuota nvidia.com/gpu: 2, matching the fabricated capacity) + gpu-jobs Namespace +
LocalQueue. First apply used `kueue.x-k8s.io/v1beta1` -- got a deprecation warning, checked
`kubectl explain --api-version=kueue.x-k8s.io/v1beta2`, confirmed identical field schema, bumped to v1beta2:

```bash
kubectl --context kind-inference-poc apply -f poc/k8s/kueue-lab/fake-gpu-queue.yaml
kubectl --context kind-inference-poc get clusterqueue gpu-cluster-queue -o yaml
kubectl --context kind-inference-poc -n gpu-jobs get localqueue gpu-queue -o yaml
# Result: both Active=True, "Can admit new workloads" / "Can submit new workloads to localQueue"
```

### DRA -- checked, deliberately skipped for this pass

```bash
kubectl --context kind-inference-poc api-resources --api-group=resource.k8s.io
# Result: DeviceClass/ResourceClaim/ResourceClaimTemplate/ResourceSlice already available (resource.k8s.io/v1) --
# no feature-gate changes needed on this kind cluster

gh api repos/kubernetes-sigs/kueue/contents/keps/2941-DRA/kep.yaml --jq '.content' | base64 -d | grep -iE "stage|status|latest-milestone"
# Result: DRA support in Kueue is "beta" as of v0.19 (the version installed here)
gh api repos/kubernetes-sigs/dra-example-driver --jq '.description, .archived'
# Result: real, non-archived reference driver for hardware-free DRA testing -- exists as a future option,
# not pursued now (classic extended-resource model is a different, separate thing from DRA)
```

### Quota-enforcement test -- as a pytest case, not a standalone manifest

```bash
# poc/tests/helpers.py -- added input_text param to kubectl() for piping YAML via stdin (kubectl apply -f -)
# poc/tests/test_10_kueue.py -- submits 3 Jobs (unique name suffix per run) x 1 fake GPU each against
# the 2-GPU ClusterQueue quota; polls spec.suspend on each (Kueue admission is async); asserts exactly
# 1 of 3 stays suspended. Cleans up jobs in a finally block regardless of outcome.
pytest -v
# Result: 25 passed, 4 skipped (vLLM not running) -- test_10 passed on the first run
```

## Phase 5 — Control Plane D: observability

Scoped up front: build Prometheus + Grafana now, defer KEDA to end of POC, defer Argo CD/OpenBao/Trivy/Falco
(ask again later).

### kube-prometheus-stack install

```bash
# poc/k8s/prometheus-lab/Chart.yaml -- dependency kube-prometheus-stack 91.4.1, prometheus-community repo.
cd poc/k8s/prometheus-lab
helm dependency update
kubectl --context kind-inference-poc create namespace monitoring
helm --kube-context kind-inference-poc install prometheus . -n monitoring
kubectl --context kind-inference-poc -n monitoring get pods
# Result: prometheus/grafana/alertmanager-disabled/kube-state-metrics/node-exporter all Running
```

`poc/k8s/prometheus-lab/values.yaml` -- Alertmanager disabled (no alerting use case yet);
`kubeControllerManager`/`kubeScheduler`/`kubeEtcd`/`kubeProxy` disabled (on `kind` these run as static pods
bound to `127.0.0.1` only, not reachable by ServiceMonitor-based scraping -- well-known `kind` limitation,
confirmed via their target status showing `down` before disabling); `kubeApiServer`/`kubelet`/`coreDns` left
enabled since those *are* reachable.

Considered loosening `serviceMonitorSelectorNilUsesHelmValues`/`podMonitorSelectorNilUsesHelmValues` to
`false` for easier cluster-wide discovery -- rejected after checking the actual blast radius (it only
changes which ServiceMonitor/PodMonitor *objects* get watched, not raw pods; nothing else in the cluster
creates such objects today, so the practical delta was zero, but it's a global setting affecting any future
chart too). Left the default alone and labeled our own new objects with `release: prometheus` instead.

### Persistent storage for Prometheus

```bash
# Checked a real storage provisioner exists before adding a PVC (kind's default StorageClass):
kubectl --context kind-inference-poc get storageclass
# Result: "standard" (rancher.io/local-path), marked (default)
kubectl --context kind-inference-poc -n local-path-storage get pods
# Result: local-path-provisioner Running
kubectl --context kind-inference-poc -n identity-tenancy get pvc postgres-data
# Result: Bound, 10+ days old -- same StorageClass already proven working
```

Added `storageSpec.volumeClaimTemplate` (2Gi, no `storageClassName` -- `standard` is the cluster default) to
`poc/k8s/prometheus-lab/values.yaml`. Without this, `Prometheus.spec.storage` is unset and the TSDB lives on
ephemeral storage tied to the pod's lifecycle -- the `retention: 10d` setting would be meaningless, since a
pod restart (which happens on every `podman machine` pause/resume in this project) wipes all history.

```bash
helm --kube-context kind-inference-poc upgrade prometheus . -n monitoring
kubectl --context kind-inference-poc -n monitoring get pvc
# Result: Bound
```

### ServiceMonitor/PodMonitor for our own components

```bash
# poc/k8s/prometheus-lab/service-monitors.yaml -- ServiceMonitor for vllm-mock-pool (port 8000, /metrics),
# PodMonitor for agentgateway's data-plane pod (port "metrics"=15020, no Service exposed it by name yet).
# Both labeled release: prometheus to match the Operator's default discovery selector.
kubectl --context kind-inference-poc apply -f service-monitors.yaml
kubectl --context kind-inference-poc -n monitoring port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 &
curl -s 'http://localhost:9090/api/v1/targets' | jq '.data.activeTargets[] | select(.scrapePool | contains("vllm-mock-pool"))'
# Result: empty -- zero targets discovered, not a failed scrape
```

### Bug 1: vllm-mock-pool ServiceMonitor matched zero targets

```bash
kubectl --context kind-inference-poc -n inference-poc get svc vllm-mock-pool -o jsonpath='{.metadata.labels}'
# Result: {} -- empty. spec.selector (how the Service finds its pods) was set, but metadata.labels
# (what ServiceMonitor.spec.selector actually matches against) was never set.
```

Considered switching the ServiceMonitor to a match-all/empty selector instead of fixing the label --
rejected: `inference-poc` namespace also has the unrelated `inference-gateway` Service (HTTP traffic on port
80), which a match-all selector would incorrectly also pick up. Fixed the actual root cause instead: added
`metadata.labels: {app: vllm-mock-pool}` to `poc/k8s/mock-servers/service.yaml`.

```bash
kubectl --context kind-inference-poc apply -f poc/k8s/mock-servers/service.yaml
kubectl --context kind-inference-poc -n inference-poc get svc vllm-mock-pool -o jsonpath='{.metadata.labels}'
# Result: {} -- still empty! File on disk confirmed correct (Read tool + cat). kubectl apply reported
# "unchanged" without applying the new field.
kubectl --context kind-inference-poc -n inference-poc label svc vllm-mock-pool app=vllm-mock-pool
# Result: label applied immediately, persists -- proves nothing was stripping it, the issue was specifically
# in how `kubectl apply` computed its diff for a newly-added field on an existing object.
kubectl --context kind-inference-poc apply -f poc/k8s/mock-servers/service.yaml
# Result: "unchanged", label still present -- file/cluster consistency restored going forward.
curl -s 'http://localhost:9090/api/v1/targets' | jq '.data.activeTargets[] | select(.scrapePool | contains("vllm-mock-pool")) | .health'
# Result: "up" x2 (mock-a, mock-b)
```

### Bug 2: agentgateway's /metrics rejects the entire scrape

```bash
curl -s 'http://localhost:9090/api/v1/targets' | jq '.data.activeTargets[] | select(.scrapePool | contains("agentgateway"))'
# Result: health "down", lastError: "invalid metric type \"info\""
POD=$(kubectl --context kind-inference-poc -n inference-poc get pods -l gateway.networking.k8s.io/gateway-name=inference-gateway -o jsonpath='{.items[0].metadata.name}')
kubectl --context kind-inference-poc -n inference-poc port-forward pod/$POD 15020:15020 &
curl -s -D- http://localhost:15020/metrics | grep -i content-type
# Result: "text/plain;charset=utf-8"
curl -s http://localhost:15020/metrics | grep -A1 "TYPE agentgateway_build"
# Result: "# TYPE agentgateway_build info" -- "info" is an OpenMetrics-only type, illegal under text/plain,
# which is why Prometheus rejects the WHOLE scrape (not just this one line) -- zero metrics collected,
# including agentgateway_gen_ai_client_token_usage (the metric that proved criterion 4 in Phase 2).
curl -s -H "Accept: application/openmetrics-text;version=1.0.0" -D- http://localhost:15020/metrics -o /dev/null | grep -i content-type
# Result: still "text/plain;charset=utf-8" -- agentgateway ignores Accept-header content negotiation entirely
```

Checked agentgateway's own official monitoring chart (`helm show values oci://cr.agentgateway.dev/charts/agentgateway`)
-- has `monitoring.enabled`/`serviceMonitor.enabled`/`proxy.podMonitor.enabled` toggles, but these just wire
up scraping of the same broken endpoint; doesn't fix the underlying bug. Searched GitHub issues on
`agentgateway/agentgateway` for "openmetrics"/"content-type"/"prometheus text/plain" -- no existing report.

Tried two Prometheus-side CRD fields before concluding it needs a workaround, both empirically confirmed not
to help:

```bash
kubectl --context kind-inference-poc explain podmonitor.spec.fallbackScrapeProtocol
# "defines the protocol to use if a scrape returns blank, unparseable, or otherwise invalid Content-Type.
#  It requires Prometheus >= v3.0.0." -- checked bundled version:
kubectl --context kind-inference-poc -n monitoring get pods -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].spec.containers[*].image}'
# Result: quay.io/prometheus/prometheus:v3.14.0-distroless -- meets the requirement, tested anyway:
# (added fallbackScrapeProtocol: OpenMetricsText1.0.0 to the PodMonitor's spec -- NOT per-endpoint, a
#  top-level spec field)
kubectl --context kind-inference-poc apply -f service-monitors.yaml
curl -s 'http://localhost:9090/api/v1/targets' | jq -r '.data.activeTargets[] | select(.scrapePool|contains("agentgateway")) | .lastError'
# Result: still "invalid metric type \"info\"" -- doesn't help. Confirmed why: agentgateway's Content-Type
# header IS present and valid (maps to a real, recognized format), so this fallback -- which only triggers
# on blank/unrecognized Content-Type -- never activates.

# Also tested scrapeProtocols (controls Prometheus's outbound Accept-header preference order), using the
# exact Accept string from agentgateway's own Rust unit test suite (confirmed via `gh api` fetch of
# crates/agentgateway/src/management/metrics_server.rs at the deployed v1.5.0-beta.1 tag):
curl -s -D- -o /dev/null "http://localhost:15020/metrics" \
  -H "Accept: application/openmetrics-text;version=1.0.0;escaping=allow-utf-8;q=0.5,application/openmetrics-text;version=0.0.1;q=0.4,text/plain;version=1.0.0;escaping=allow-utf-8;q=0.3,text/plain;version=0.0.4;q=0.2,*/*;q=0.1" \
  | grep -i content-type
# Result: still "text/plain;charset=utf-8", even with this exact header -- confirmed at the deployed tag,
# the ContentType enum only recognizes "text/plain" -> PlainText or a protobuf media type; there's no
# openmetrics-text case at all in the negotiation logic. Both fields removed from service-monitors.yaml
# afterward (dead config, would only confuse a future reader).
```

Root cause, confirmed by reading `crates/agentgateway/src/management/metrics_server.rs` at both the deployed
tag and `main`: agentgateway uses the Rust `prometheus_client` crate, whose `encoding::text::encode`
function is its *only* text encoder -- OpenMetrics-only by design, no classic-Prometheus-0.0.4 encoder
exists in that crate at all. `MetricsFormat::PlainText` and `MetricsFormat::OpenMetricsText` both call that
same encoder, but `PlainText` hardcodes `Content-Type: text/plain;charset=utf-8` regardless. Still true on
`main` as of 2026-09 (just renamed the import) -- not fixed by a version bump.

### Fix: standalone Content-Type-correcting relay

Can't add a sidecar container to agentgateway's own pod -- it's managed by the GatewayClass controller,
which reverts direct Deployment/pod-spec patches within seconds (established in Phase 3 getting trace logs
working). Built a standalone relay instead:

```bash
# poc/k8s/agentgateway-metrics-relay/upstream-service.yaml -- plain Service selecting the gateway's own
# pods by their existing label (gateway.networking.k8s.io/gateway-name=inference-gateway), port 15020.
# Independent object, not touching the managed Deployment -- safe from controller reversion.
# poc/k8s/agentgateway-metrics-relay/relay.py -- fetches upstream /metrics verbatim, re-serves the exact
# same bytes under Content-Type: application/openmetrics-text;version=1.0.0;charset=utf-8.
# poc/k8s/agentgateway-metrics-relay/configmap.yaml -- generated via
# kubectl create configmap agentgateway-metrics-relay-code --from-file=relay.py --dry-run=client -o yaml
# poc/k8s/agentgateway-metrics-relay/deployment.yaml -- python:3.12-slim, same pattern as mock_server.py.
kubectl --context kind-inference-poc -n inference-poc apply -f poc/k8s/agentgateway-metrics-relay/
kubectl --context kind-inference-poc -n inference-poc port-forward svc/agentgateway-metrics-relay 9114:9114 &
curl -s -D- -o /tmp/relay_body.txt http://localhost:9114/metrics | grep -i content-type
# Result: "application/openmetrics-text;version=1.0.0;charset=utf-8"
grep "agentgateway_gen_ai_client_token_usage" /tmp/relay_body.txt | head -1
# Result: real histogram data present -- the metric that proved criterion 4, now scrapable
```

Swapped the broken PodMonitor for a ServiceMonitor pointed at the relay's Service in
`poc/k8s/prometheus-lab/service-monitors.yaml`, deleted the stale PodMonitor object (removing it from the
file doesn't delete it from the cluster):

```bash
kubectl --context kind-inference-poc apply -f poc/k8s/prometheus-lab/service-monitors.yaml
kubectl --context kind-inference-poc -n inference-poc delete podmonitor agentgateway-data-plane
```

### RLS: no /metrics endpoint at all

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/metrics   # via existing RLS port-forward
# Result: 404 -- only /healthcheck exists. USE_STATSD=false means gostats only ever wrote to stdout logs.
```

Standard fix for a statsd-only emitter: `prom/statsd-exporter` sidecar.

```bash
# poc/k8s/rls/statsd-exporter.yaml -- Deployment + Service, ports 9125 (statsd) / 9102 (Prometheus /metrics)
kubectl --context kind-inference-poc -n identity-tenancy apply -f poc/k8s/rls/statsd-exporter.yaml
# Result: ErrImagePull on prom/statsd-exporter:v0.30.0 -- checked Docker Hub tags directly, v0.30.0 only
# exists as "-distroless"; switched to v0.31.0 (a plain tag that does exist).
```

Flipped RLS to use it:

```bash
# poc/k8s/rls/rls.yaml -- USE_STATSD: "true", STATSD_HOST: statsd-exporter, STATSD_PORT: "9125"
kubectl --context kind-inference-poc -n identity-tenancy apply -f poc/k8s/rls/rls.yaml
kubectl --context kind-inference-poc -n identity-tenancy logs -l app=rls --tail=10
# Result: "statsd connection error: dial tcp 10.96.37.97:9125: i/o timeout" -- RLS's gostats client dials
# over TCP by default, but the Service only exposed port 9125 as UDP.
```

```bash
# Added a second ports entry (statsd-tcp, same port 9125, protocol: TCP) to the Service.
kubectl --context kind-inference-poc -n identity-tenancy apply -f poc/k8s/rls/statsd-exporter.yaml
kubectl --context kind-inference-poc -n identity-tenancy get svc statsd-exporter -o jsonpath='{.spec.ports}'
# Result: only 2 of the 3 ports present (statsd-udp, metrics) -- the TCP entry silently dropped. Same class
# of "kubectl apply diffing quirk" as Bug 1 above: spec.ports' strategic-merge-patch key is "port", not
# "name", so two entries sharing port: 9125 collide during the patch computation.
kubectl --context kind-inference-poc -n identity-tenancy delete svc statsd-exporter
kubectl --context kind-inference-poc -n identity-tenancy create -f poc/k8s/rls/statsd-exporter.yaml
kubectl --context kind-inference-poc -n identity-tenancy get svc statsd-exporter -o jsonpath='{.spec.ports}'
# Result: all 3 ports present now (delete + create instead of patch-apply)
```

```bash
kubectl --context kind-inference-poc -n identity-tenancy logs -l app=rls --tail=15
# Result: STILL "dial tcp ...:9125: i/o timeout" errors, but statsd-exporter's own /metrics already showed
# real ratelimit_* data (statsd_exporter_tcp_connections_total: 1) -- one connection had succeeded and kept
# delivering data, but repeated new dial attempts kept failing. Hypothesis: RLS's gostats client cached the
# Service's OLD ClusterIP (resolved before the delete+recreate above assigned a new one).
kubectl --context kind-inference-poc -n identity-tenancy rollout restart deployment/rls
kubectl --context kind-inference-poc -n identity-tenancy logs -l app=rls --tail=20
# Result: clean -- zero "dial tcp" errors after a fresh pod re-resolved DNS. Confirmed stable over a 30s
# watch window; statsd_exporter_samples_total climbing steadily (1866 -> 14562), zero connection errors.
```

Added a ServiceMonitor for statsd-exporter (namespace `identity-tenancy`) alongside the relay's ServiceMonitor
in `poc/k8s/prometheus-lab/service-monitors.yaml`.

### End-to-end verification

```bash
curl -s 'http://localhost:9090/api/v1/targets' | jq -r '.data.activeTargets[] | select(.scrapePool|test("agentgateway|statsd")) | "\(.scrapePool) \(.health)"'
# Result: serviceMonitor/inference-poc/agentgateway-metrics-relay/0 up
#         serviceMonitor/identity-tenancy/statsd-exporter/0 up
curl -s 'http://localhost:9090/api/v1/query?query=agentgateway_gen_ai_client_token_usage_sum' | jq '.data.result[0].value'
curl -s 'http://localhost:9090/api/v1/query?query=ratelimit_service_config_load_success' | jq '.data.result[0].value'
# Result: both queries return real values -- full pipeline confirmed working, not just "target up"
```

### Captured as an automated test, not just manual curl+kubectl inspection

```bash
# poc/tests/conftest.py -- added prometheus_port fixture (svc/prometheus-kube-prometheus-prometheus, 9090)
# poc/tests/test_11_metrics.py -- confirms all three new targets show `up` via Prometheus's own
# /api/v1/targets API, and that real (non-placeholder) data is queryable for each: the mocks'
# vllm:kv_cache_usage_perc, a live request's agentgateway_gen_ai_client_token_usage_sum through the relay,
# and RLS's ratelimit_service_config_load_success through statsd-exporter -- plus that Prometheus's PVC is
# actually Bound, not ephemeral.
pytest -v
# Result: 30 passed, 4 skipped (vLLM not running) -- test_11's 5 new tests all pass, zero regressions
```

## Phase 6 — Control Plane A: billing

Scoped up front: build Kafka + ClickHouse + OpenMeter + the collector-boundary decision now; Stripe test
mode explicitly skipped (not required by acceptance criterion 5, which only asks for the event-capture
pipeline); collector boundary = agentgateway -> OTel Collector -> Kafka (the plan's own recommendation).

Folder convention changed this phase: no more `-lab` suffix on new component folders (`poc/k8s/kafka/`,
`clickhouse/`, `otel/`, `openmeter/`, not `kafka-lab/` etc.) -- existing `-lab` folders to be renamed later,
not part of this phase.

### Kafka (Strimzi Operator, KRaft mode)

```bash
helm repo add strimzi https://strimzi.io/charts/
helm search repo strimzi/strimzi-kafka-operator --versions
# Result: 1.2.0 latest -- ZooKeeper support removed as of 0.46+, this pin is KRaft-only by necessity
```

`poc/k8s/kafka/Chart.yaml` -- dependency `strimzi-kafka-operator` 1.2.0.

```bash
cd poc/k8s/kafka && helm dependency update
kubectl --context kind-inference-poc create namespace billing
helm --kube-context kind-inference-poc install kafka-operator . -n billing
kubectl --context kind-inference-poc -n billing get pods
# Result: strimzi-cluster-operator Running
```

`poc/k8s/kafka/kafka-cluster.yaml` -- adapted from Strimzi's own `examples/kafka/kafka-single-node.yaml`
(fetched via `gh api repos/strimzi/strimzi-kafka-operator/contents/examples/kafka/kafka-single-node.yaml?ref=1.2.0`),
single dual-role (broker+controller) `KafkaNodePool`, PVC size dropped 100Gi -> 5Gi, plus a `usage-events`
`KafkaTopic` for an initial smoke test.

```bash
kubectl --context kind-inference-poc -n billing apply -f poc/k8s/kafka/kafka-cluster.yaml
kubectl --context kind-inference-poc -n billing wait --for=condition=Ready kafka/kafka --timeout=180s
# Result: Ready. usage-events topic Ready too.

# Real produce/consume smoke test, not just trusting Ready status:
kubectl --context kind-inference-poc -n billing exec -i kafka-dual-role-0 -c kafka -- bash -c \
  "echo 'hello-from-phase-6' | bin/kafka-console-producer.sh --bootstrap-server localhost:9092 --topic usage-events"
kubectl --context kind-inference-poc -n billing exec kafka-dual-role-0 -c kafka -- \
  bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic usage-events --from-beginning --max-messages 1 --timeout-ms 10000
# Result: "hello-from-phase-6" round-tripped correctly
```

### ClickHouse (Altinity Operator)

```bash
helm repo add altinity https://helm.altinity.com
helm search repo altinity
# Result: altinity/clickhouse 0.3.13 -- bundles the Altinity Operator as a dependency AND creates the
# actual ClickHouseInstallation CR (one chart, not two) -- same operator OpenMeter's own bundled dev-mode
# setup uses internally (confirmed later via its chart values), so a proven-compatible pairing.
```

`poc/k8s/clickhouse/Chart.yaml` + `values.yaml` -- `replicasCount`/`shardsCount` already default to 1/1;
`persistence.size` dropped 10Gi -> 2Gi; `defaultUser.password: clickhouse-poc-only`.

```bash
cd poc/k8s/clickhouse && helm dependency update
helm --kube-context kind-inference-poc install clickhouse . -n billing
kubectl --context kind-inference-poc -n billing get chi
# Result: Completed, 1 cluster, 1 host

# Real query test, not just trusting Completed status:
kubectl --context kind-inference-poc -n billing exec chi-clickhouse-clickhouse-0-0-0 -- \
  clickhouse-client --user default --password clickhouse-poc-only --query "SELECT version(), 1+1"
# Result: 25.3.6.10034.altinitystable  2 -- real query works
```

### OTel Collector -- researching agentgateway's real OTLP shape before wiring blind

Checked whether Prometheus-scraping (already working from Phase 5) could double as the billing-event
source -- rejected: a scraped cumulative counter/histogram has no per-request granularity, and the wiki's
own design wants one event per request. Needed a genuine per-request signal (trace/log), not a metric.

```bash
# agentgateway's own reference OTel-stack architecture (agentgateway.dev/docs/kubernetes/latest/observability/otel-stack/):
# access-logs AgentgatewayPolicy -> OTel logs collector -> Loki; tracing AgentgatewayPolicy -> OTel traces
# collector -> Tempo. Confirmed token usage appears in agentgateway's structured access logs with real
# token fields (schema/cel.md, fetched via gh api):
gh api repos/agentgateway/agentgateway/contents/schema/cel.md --jq '.content' | base64 -d > /tmp/agentgateway-cel.md
grep -n '^|`llm\.' /tmp/agentgateway-cel.md
# Result: llm.inputTokens, llm.outputTokens, llm.totalTokens, llm.requestModel, llm.provider, llm.cost.total
# (a full pre-computed USD cost catalog even), etc. -- no requestId/traceId field under `llm.` at all.
```

`poc/k8s/otel/Chart.yaml` -- dependency `open-telemetry/opentelemetry-collector` 0.173.1.
`poc/k8s/otel/values.yaml` -- `image.repository: otel/opentelemetry-collector-contrib` (the plain/core
image doesn't include the Kafka exporter needed downstream), tag `0.160.0` (matches this chart's own
default appVersion). Used `alternateConfig`, not `config` -- the chart's own values.yaml documents a real
Helm bug (helm/helm#12879) where `config` only partially merges with chart defaults when used as a
subchart (our case), silently mangling array-valued keys like `service.pipelines.logs.processors`.

```bash
cd poc/k8s/otel && helm dependency update
helm --kube-context kind-inference-poc install otel . -n billing
# Result: 1/1 Running, debug-only pipeline (verification step, not the final config)
```

`poc/k8s/otel/access-log-policy.yaml` -- `AgentgatewayPolicy` targeting the `inference-gateway` Gateway,
using `url:` (not `backendRef:`) to avoid a cross-namespace `ReferenceGrant` for this verification pass.

```bash
kubectl --context kind-inference-poc apply -f poc/k8s/otel/access-log-policy.yaml
# Real request through the existing gateway/mock pipeline, then read the Collector's debug-exporter output:
kubectl --context kind-inference-poc -n inference-poc port-forward svc/inference-gateway 18080:80 &
curl -s -X POST http://localhost:18080/v1/chat/completions -H "x-api-key: pytest-otel-verify-1" \
  -H "Content-Type: application/json" -d '{"model":"mock","messages":[{"role":"user","content":"..."}],"max_tokens":42}'
kubectl --context kind-inference-poc -n billing logs deploy/otel-opentelemetry-collector --tail=100
# Result: real LogRecord with gen_ai.usage.input_tokens=6/output_tokens=4 (matching actual usage.total_tokens
# in the HTTP response) AND agentgateway's own default GenAI semantic-convention attributes already present
# -- but Trace ID/Span ID both EMPTY. No incoming traceparent header, and randomSampling defaults to
# disabled, so agentgateway never initiates a trace on its own.
```

Added `frontend.tracing` (same policy object) with `randomSampling: "1.0"` to force-sample every request --
without this there's no free per-request unique identifier at all for the event's `id`.

```bash
# Re-tested: Trace ID/Span ID now populated (also duplicated as trace.id/span.id log attributes).
# Also tested apiKey.key.unredacted() for "subject" -- came back empty. This project's auth is a custom
# RLS check (poc/k8s/rls/agentgateway-policy.yaml), not agentgateway's built-in API-key-policy CEL context.
# Switched to request.headers['x-api-key'] directly -- populated correctly on retest.
```

### Transform: agentgateway's access log -> CloudEvents JSON -> Kafka

```bash
gh api repos/open-telemetry/opentelemetry-collector-contrib/contents/exporter/kafkaexporter/README.md \
  --jq '.content' | base64 -d > /tmp/kafkaexporter-readme.md
# Result: `raw` encoding -- non-byte-array log body gets JSON-serialized as-is, no manual string-building
# needed for the outer envelope.
```

First attempt used `context: log` (Advanced Config) with bare `attributes[...]` paths, assuming the context
key drops the need for a `log.` prefix -- wrong. Confirmed via the transform processor's own README
("Context inference" section): the `log.` prefix is what determines the context, always required
regardless of which config style is used.

Nested map literals (`data: {...}` inside the outer envelope `{...}`) hit a real, open OTTL bug
(open-telemetry/opentelemetry-collector-contrib#37405, map literals nested inside other literals). Worked
around by building two separate flat map literals and nesting via a `log.cache["envelope"]["data"] =
log.cache["data"]` key-path assignment instead of literal nesting.

```yaml
# poc/k8s/otel/values.yaml -- transform/usage_events processor, log_statements (first version):
- set(log.cache["data"], {"model": ..., "input_tokens": ..., ...}) where log.attributes["protocol"] == "llm"
- set(log.cache["envelope"], {"specversion": "1.0", "id": log.attributes["trace.id"], ..., "time": FormatTime(log.observed_time, "%Y-%m-%dT%H:%M:%S.%fZ")}) where ...
- set(log.cache["envelope"]["data"], log.cache["data"]) where ...
- set(log.body, log.cache["envelope"]) where ...
```

```bash
helm --kube-context kind-inference-poc upgrade otel . -n billing
# Result: "cannot unmarshal the configuration: mapping values are not allowed in this context" -- YAML
# parsed the unquoted {"key": value} colons as block-mapping syntax. Fixed by single-quoting each OTTL
# statement as a YAML string.
```

Added `kafka/usage_events` exporter (`brokers`, `logs.topic: om_default_events`, `logs.encoding: raw`) and
wired it into the logs pipeline alongside `debug`.

```bash
helm --kube-context kind-inference-poc upgrade otel . -n billing
# Real request through the gateway again, then consume the topic directly:
kubectl --context kind-inference-poc -n billing exec kafka-dual-role-0 -c kafka -- \
  bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic om_default_events --from-beginning --max-messages 3 --timeout-ms 10000
# Result: real CloudEvents-shaped JSON landed --
# {"data":{"input_tokens":6,"model":"mock","output_tokens":4,"provider":"custom","total_tokens":10},
#  "id":"d411ed4ba0b6b213f04ad49c28a9c8af","source":"agentgateway","specversion":"1.0",
#  "subject":"customer-kafka-e2e-test","time":"2026-09-28T09:49:27.713985Z","type":"tokens_used"}
# Matches the real response's usage exactly. Collector-boundary decision (criterion 5) verified end-to-end.
```

Swapped the debug-only pipeline for the real one, deleted the temporary `access-log-policy.yaml`
verification comments (kept the policy itself, now load-bearing).

### OpenMeter (self-hosted)

```bash
gh api "repos/openmeterio/openmeter/contents/config.example.yaml" --jq '.content' | base64 -d > /tmp/openmeter-config.example.yaml
# ingest.kafka.eventsTopicTemplate default: "om_%s_events" -- %s = OpenMeter's own tenant "namespace"
# concept (not k8s), confirmed default value "default" via app/config/namespace.go source. Topic name
# above (om_default_events) wasn't a guess.
```

`poc/k8s/openmeter/Chart.yaml` -- dependency `oci://ghcr.io/openmeterio/helm-charts/openmeter`
`1.0.0-beta.138` (latest available -- no stable `1.0` tag exists upstream yet, confirmed via the GHCR tags
API directly since `helm search` doesn't work against OCI repos without a version).

```bash
# Dedicated Postgres db/user for OpenMeter, reusing the existing Phase-3 instance rather than a second one:
POD=$(kubectl --context kind-inference-poc -n identity-tenancy get pods -l app=postgres -o jsonpath='{.items[0].metadata.name}')
kubectl --context kind-inference-poc -n identity-tenancy exec "$POD" -- psql -U identity_tenancy -d identity_tenancy -c "CREATE DATABASE openmeter;"
kubectl --context kind-inference-poc -n identity-tenancy exec "$POD" -- psql -U identity_tenancy -d identity_tenancy -c "CREATE USER openmeter WITH PASSWORD 'openmeter-poc-only';"
kubectl --context kind-inference-poc -n identity-tenancy exec "$POD" -- psql -U identity_tenancy -d identity_tenancy -c "GRANT ALL PRIVILEGES ON DATABASE openmeter TO openmeter;"
kubectl --context kind-inference-poc -n identity-tenancy exec "$POD" -- psql -U identity_tenancy -d identity_tenancy -c "ALTER DATABASE openmeter OWNER TO openmeter;"
```

`poc/k8s/openmeter/values.yaml` -- `kafka.enabled`/`clickhouse.enabled: false` (point at our own instead of
the bundled dev pair), `config.postgres.url` (the new db/user above -- no bundled dev-mode toggle exists
for Postgres at all in this chart), `config.aggregation.clickhouse.*` (native protocol, port 9000, not the
8123 HTTP port), `config.sink.dedupe` (Redis driver, pointed at the existing Valkey rather than a new
Redis, `database: 1` to avoid colliding with RLS's own keys on db 0).

```bash
cd poc/k8s/openmeter && helm dependency update
helm --kube-context kind-inference-poc install openmeter . -n billing
# Result: ServiceAccount "strimzi-cluster-operator" ... exists and cannot be imported -- kafka.enabled:
# false alone isn't enough; the chart has a SEPARATE kafka.operator.install / clickhouse.operator.install
# toggle that still tries installing its own Strimzi/Altinity operator release, colliding with ours.
```

Set `kafka.operator.install: false` / `clickhouse.operator.install: false` too, then hit a sequence of real
issues, each fixed and re-verified:

```bash
# 1. panic: no meters configured -- needs at least one meter defined; added config.meters (slug
#    tokens_total, eventType tokens_used, aggregation SUM, valueProperty $.total_tokens) matching the
#    CloudEvents shape the OTel Collector transform produces.
#
# 2. ingest.kafka.broker still connecting to 127.0.0.1:29092 (bundled dev default) despite
#    ingest.kafka.brokers being set -- wrong key name. Confirmed via source
#    (app/config/ingest.go): v.SetDefault(prefixer("kafka.broker"), "127.0.0.1:29092") -- SINGULAR
#    "broker", not plural "brokers". Both api and sink-worker share this same key.
#
# 3. ClickHouse "Authentication failed: password is incorrect, or there is no user with such name" --
#    reproduced the identical error via direct clickhouse-client over the network (not localhost),
#    confirmed the actual stored password via `kubectl get secret clickhouse-credentials` matched exactly
#    what we set. Real cause: default/networks/ip: 127.0.0.1/32 on the CHI -- silently rejects any
#    non-localhost connection with the SAME generic auth-failure message. Fixed via
#    clickhouse.defaultUser.allowExternalAccess: true (poc/k8s/clickhouse/values.yaml).
#
# 4. Same auth error persisted even after the network fix -- turned out to be a SEPARATE issue:
#    OpenMeter's client sends the target database as part of its initial connection handshake, and
#    that database ("openmeter") didn't exist yet -- the combined (user, password, nonexistent-database)
#    tuple gets rejected with the same generic code, not a distinct "unknown database" error like the
#    CLI tool gives when switching database as a separate step. Fixed by creating the database directly:
kubectl --context kind-inference-poc -n billing exec chi-clickhouse-clickhouse-0-0-0 -- \
  clickhouse-client --user default --password clickhouse-poc-only --query "CREATE DATABASE IF NOT EXISTS openmeter"
```

```bash
kubectl --context kind-inference-poc -n billing delete pod -l app.kubernetes.io/instance=openmeter
kubectl --context kind-inference-poc -n billing logs deploy/openmeter-api --tail=10
# Result: "default namespace created", "meters successfully created" -- both components genuinely healthy
```

### Real end-to-end test, and the sink-worker crash chain

```bash
# Real request through the gateway, then query OpenMeter's own meter API:
curl -s "http://localhost:8888/api/v1/meters/tokens_total/query?subject=..."
# Result: {"data": []} -- empty. Checked ClickHouse directly: om_events also empty.
```

Traced this through several distinct, real issues layered on top of each other (each confirmed via direct
source reading at the exact deployed tag, `gh api ...?ref=v1.0.0-beta.138`, not "main" HEAD -- the running
binary's actual logic repeatedly differed from "main"):

```bash
# Bug A: missing Kafka message HEADER. OpenMeter's sink-worker reads the tenant namespace from a Kafka
# message header (kafkaingest.HeaderKeyNamespace = "namespace"), not the JSON body -- our raw kafkaexporter
# writes no custom headers at all. Missing header -> message correctly marked to be dropped internally.
#
# Bug B: real upstream crash. dedupeSinkMessages at the deployed tag (internal/sink/sink.go:811) doesn't
# check message processing state before dereferencing .Serialized.Id -- ANY dropped message in a flush
# batch panics the whole sink-worker with a nil-pointer SIGSEGV, not just skips it. Confirmed fixed on
# "main" (adds a switch on event.Status.State) but not backported to this beta tag.
gh api "repos/openmeterio/openmeter/contents/internal/sink/sink.go?ref=v1.0.0-beta.138" --jq '.content' | base64 -d > /tmp/sink-real.go
```

Added `record_headers: [{name: namespace, value: default}]` to the kafka exporter (first attempt used a
plain YAML map, which failed: `'record_headers' source data must be an array or slice, got map` --
`RecordHeader` is `{name, value}` objects in a list, confirmed via the exporter's Go source).

```bash
helm --kube-context kind-inference-poc upgrade otel . -n billing
# Real request -- crashed again, but this time confirmed genuinely processing (no race with a restart).
gh api "repos/openmeterio/openmeter/contents/internal/ingest/kafkaingest/serializer/serializer.go?ref=v1.0.0-beta.138" \
  --jq '.content' | base64 -d
# Result: Bug C -- wire-schema mismatch. CloudEventsKafkaPayload requires `Time int64` (raw Unix seconds,
# not RFC3339 string) and `Data string` (JSON-ENCODED STRING, not a nested object). Either mismatch fails
# json.Unmarshal outright on the consumer side, hitting Bug B's same unguarded crash for a different reason.
```

Rewrote the transform: `UnixSeconds(log.observed_time)` for `time`; `data` built via a manually-escaped
`Concat()` JSON string (OTTL has no map-to-JSON-string function) instead of a nested map -- this also
collapsed the whole transform back to a single statement, since a string value doesn't hit the nested-map-
literal OTTL bug from earlier.

```bash
# Bug D: poison-pill topic. Once malformed messages land, the consumer group never commits an offset past
# them -- every restart replays from the beginning and crashes on the same first bad message again.
kubectl --context kind-inference-poc -n billing exec kafka-dual-role-0 -c kafka -- \
  bin/kafka-topics.sh --bootstrap-server localhost:9092 --delete --topic om_default_events
# Also needed: restart the OTel Collector itself after topic delete+recreate -- its Kafka client caches
# topic metadata by internal ID, and rejects the recreated topic (same name, new ID) with
# "UNKNOWN_TOPIC_ID: This server does not host this topic ID.", not a name-based error.
kubectl --context kind-inference-poc -n billing delete pod -l app.kubernetes.io/instance=otel
kubectl --context kind-inference-poc -n billing delete pod -l app.kubernetes.io/instance=openmeter
```

```bash
# Clean final test -- consumer subscribed and assigned BEFORE sending, no race:
curl -s -X POST http://localhost:18080/v1/chat/completions -H "x-api-key: customer-openmeter-v2-fix" \
  -H "Content-Type: application/json" -d '{"model":"mock","messages":[{"role":"user","content":"..."}],"max_tokens":42}'
kubectl --context kind-inference-poc -n billing exec chi-clickhouse-clickhouse-0-0-0 -- \
  clickhouse-client --user default --password clickhouse-poc-only --database openmeter \
  --query "SELECT id, subject, type, data FROM om_events ORDER BY time DESC LIMIT 5 FORMAT Vertical"
# Result: real row -- id, subject=customer-openmeter-v2-fix, type=tokens_used,
# data={"model":"mock","provider":"custom","input_tokens":6,"output_tokens":4,"total_tokens":10}
# Zero pod restarts.

curl -s "http://localhost:8888/api/v1/meters/tokens_total/query?subject=customer-openmeter-v2-fix"
# Result: {"data": [{"value": 10, "windowStart": "...", "windowEnd": "...", "subject": "customer-openmeter-v2-fix", "groupBy": {}}]}
# value=10 matches the real response's total_tokens exactly. Full pipeline verified end-to-end:
# agentgateway -> OTel Collector -> Kafka -> OpenMeter -> ClickHouse -> correct billable usage per customer.
```

### Event schema: adding event_version, and proving idempotency for real

`poc/k8s/otel/values.yaml` -- added `"event_version":"1.0"` inside the `data` JSON string, not as a
top-level envelope field. OpenMeter's `CloudEventsKafkaPayload` struct only has fixed fields
(Id/Type/Source/Subject/Time/Data) -- an extra top-level key would just be silently discarded by
`json.Unmarshal`, never stored anywhere.

```bash
helm --kube-context kind-inference-poc upgrade otel . -n billing
# Real request, then check the stored data column:
kubectl --context kind-inference-poc -n billing exec chi-clickhouse-clickhouse-0-0-0 -- \
  clickhouse-client --user default --password clickhouse-poc-only --database openmeter \
  --query "SELECT data FROM om_events WHERE subject='customer-version-check' ORDER BY time DESC LIMIT 1"
# Result: {"event_version":"1.0","model":"mock","provider":"custom","input_tokens":6,"output_tokens":4,"total_tokens":10}
```

Manually verified the actual idempotency guarantee before writing it up as a test -- a real HTTP request
can't produce two events sharing the same trace ID (every request gets a fresh one), so simulating
redelivery means producing a raw Kafka message with a fixed `id` directly:

```bash
# kafka-console-producer's header syntax: --property parse.headers=true, format "h1:v1,...\tvalue"
cat > /tmp/dedup-test-message.txt << 'EOF'
namespace:default	{"specversion":"1.0","id":"dedup-test-manual-1","source":"agentgateway","type":"tokens_used","subject":"customer-dedup-manual","time":1790590000,"data":"{\"event_version\":\"1.0\",\"model\":\"mock\",\"provider\":\"custom\",\"input_tokens\":100,\"output_tokens\":50,\"total_tokens\":150}"}
EOF
# Produced the IDENTICAL message twice:
kubectl --context kind-inference-poc -n billing exec -i kafka-dual-role-0 -c kafka -- bin/kafka-console-producer.sh \
  --bootstrap-server localhost:9092 --topic om_default_events --property parse.headers=true < /tmp/dedup-test-message.txt
kubectl --context kind-inference-poc -n billing exec -i kafka-dual-role-0 -c kafka -- bin/kafka-console-producer.sh \
  --bootstrap-server localhost:9092 --topic om_default_events --property parse.headers=true < /tmp/dedup-test-message.txt

kubectl --context kind-inference-poc -n billing exec chi-clickhouse-clickhouse-0-0-0 -- \
  clickhouse-client --user default --password clickhouse-poc-only --database openmeter \
  --query "SELECT count(*) FROM om_events WHERE id='dedup-test-manual-1'"
# Result: 1 -- not 2. Real dedup confirmed, no crash.

curl -s "http://localhost:8888/api/v1/meters/tokens_total/query?subject=customer-dedup-manual"
# Result: {"data": [{"value": 150, ...}]} -- not 300. The redelivered duplicate never got double-counted
# in the actual billing aggregate, not just deduped at the raw-events-table level.
```

### Captured as an automated test, not just manual curl+kubectl inspection

```bash
# poc/tests/conftest.py -- added openmeter_port fixture (svc/openmeter-api, 8888 -> 80)
# poc/tests/test_12_billing.py --
#   test_real_request_produces_a_correctly_aggregated_usage_event: real request through the gateway,
#   polls OpenMeter's meter-query API, confirms the aggregated value equals the response's real total_tokens.
#   test_redelivered_event_id_is_not_double_counted: produces a raw Kafka message (same wire schema as
#   above) with a fixed id twice via kubectl exec + kafka-console-producer, confirms exactly 1 row in
#   om_events and the meter aggregate reflects the value once, not twice.
pytest -v
# Result: 32 passed, 4 skipped (vLLM not running) -- test_12's 2 new tests both pass, zero regressions
```

Not built this phase: Stripe (explicitly skipped, not deferred -- see README.md item 4). Phase 6 is
otherwise done.

## Phase 7 -- skipped, not deferred

Checked whether Phase 7 (model registry, stubbed/symbolic -- SeaweedFS + a local OCI registry) maps to any
of the 6 acceptance criteria before building it:

```bash
grep -n "Phase 7" poc/README.md
# Result: only the Phase 7 header itself -- not referenced anywhere in the acceptance criteria section
```

Criteria 1-6 map to Phases 1, 1, 2, 2, 6, 8 respectively -- nothing maps to Phase 7. It exists purely for
"every control plane in the wiki gets a stand-in," explicitly not on the real request path, proving nothing
the POC doesn't already prove without it. Skipped outright, not deferred.

Also discussed and deferred: Argo CD/OpenBao/Trivy/Falco (Phase 5's item 3 -- GitOps, secrets, supply
chain). None affect whether a request flows correctly, same as Phase 7, but unlike Phase 7 these ARE
legitimate production-operations concerns this POC just doesn't need to prove -- kept as an open, undecided
extended goal (same footing as KEDA), not skipped outright.

## Phase 8 -- acceptance criterion 6 test matrix

Criteria 1-5 each already have a specific test from the phase that built them. Phase 8 is criterion 6
specifically -- 5 named scenarios, run against the full assembled path, not individual components in
isolation.

### SSE framing, disconnect/drain, rate-limit -- poc/tests/test_13_request_edge_cases.py

```bash
# Rigorous SSE framing check: parses the raw byte stream directly (r.raw.read()), not iter_lines()'s
# higher-level view (test_01's level of rigor) -- splits on "\n\n", checks every event starts with
# "data: ", validates JSON, confirms a terminating "data: [DONE]".
#
# Disconnect/drain: reads one line of a streaming response then r.close()'s mid-stream: a follow-up
# request succeeding immediately afterward is the observable proxy for "no hung connection" from a
# black-box pytest client (can't directly inspect server-side goroutine/connection state).
pytest test_13_request_edge_cases.py -v
# Result (first pass): 2 passed, 2 failed -- invalid API key and rate-limit both got a real 200, not a
# test bug in either case (investigated both below).
```

**Invalid API key: confirmed as a genuine, pre-existing, documented gap**, not something broken by this
session's other changes:

```bash
cat poc/k8s/rls/agentgateway-policy.yaml
# Result: the file's own comment already says it -- "No real API-key authentication is wired yet (that's
# Band 3's separate APIKeyAuthentication policy, not built) -- the descriptor value is read directly from
# an x-api-key request header for now."
```

Investigated two ways to build it for real (both discussed with the user, both set aside as extended goals
rather than built now):

```bash
kubectl --context kind-inference-poc explain agentgatewaypolicy.spec.traffic.apiKeyAuthentication --api-version=agentgateway.dev/v1alpha1
# Result: only supports static keys via a Kubernetes Secret/ConfigMap (secretRef/secretSelector/
# configMapSelector) -- wrong fit, would make the Secret the source of truth instead of Postgres,
# contradicting the criterion's own wording ("Postgres/Valkey remain the source of truth").

kubectl --context kind-inference-poc explain agentgatewaypolicy.spec.traffic.extAuth --api-version=agentgateway.dev/v1alpha1
# Result: DOES support an external HTTP/gRPC authorization server per request (same architectural pattern
# already used for RLS) -- the real fix, not built. Would need a small service querying Postgres's
# api_keys table in real time, wired via extAuth.http + failureMode: FailClosed.
```

Also considered fronting agentgateway with a dedicated API gateway (Kong/Apigee) instead -- discussed with
the user and set aside: Kong doesn't read from an arbitrary existing Postgres table either (it has its own
consumer/credential store), so it would either need to become the new source of truth itself (same problem)
or run a custom plugin doing the identical Postgres check an extAuth policy would, adding a whole extra
gateway hop and Helm release without removing the actual work. Also a genuine scope expansion -- the wiki
mentions Kong exactly once, only for the developer portal (D10), never as a data-plane component.

Marked `xfail` with the full reasoning inline, not silently skipped -- the marker itself becomes the signal
to remove once this is actually built:

```python
@pytest.mark.xfail(reason="...", strict=True)
def test_invalid_api_key_is_rejected(gateway_port):
    ...
```

**Rate-limit rejection: genuinely working, but the test needed fixing, not the system.**

```bash
time (for i in $(seq 1 62); do curl -s -o /dev/null -X POST http://localhost:18080/v1/chat/completions \
  -H "x-api-key: pytest-timing-check" -d '{"model":"mock","messages":[{"role":"user","content":"hi"}]}'; done)
# Result: 1:03.93 total -- LONGER than RLS's 60-second RPM window. Sequential requests let the window
# quietly reset mid-test, so the limit is never actually hit; not a real gap.
```

Rewrote the test to fire all 61 requests concurrently (`concurrent.futures.ThreadPoolExecutor`), keeping
them within the same window; asserts on counts (60 successes, 1 rejection) since concurrent requests don't
guarantee arrival order:

```bash
pytest test_13_request_edge_cases.py -v
# Result: 3 passed, 1 xfailed -- SSE framing, disconnect/drain, and rate-limit (fixed) all pass; invalid
# API key correctly reports xfailed
```

### Upstream failure -- poc/tests/test_14_upstream_failure.py, extends mock_server.py

Two genuinely distinct failure modes: a backend reachable but erroring, and a backend completely gone.
Extended `poc/k8s/mock-servers/mock_server.py` with a `force_status` control (`POST /control
{"force_status": 500}`, `0` = disabled) for the first; used a real `kubectl scale --replicas=0` for the
second, not just an app-level simulation.

```bash
# Regenerated the ConfigMap per its own header convention after editing mock_server.py:
kubectl create configmap vllm-mock-server-code --from-file=mock_server.py --dry-run=client -o yaml > configmap.yaml
kubectl --context kind-inference-poc -n inference-poc apply -f configmap.yaml
kubectl --context kind-inference-poc -n inference-poc rollout restart deployment vllm-mock-a vllm-mock-b
```

EPP picks whichever mock reports the lower load -- doesn't know or care about error/availability state, so
forcing/scaling down only ONE mock wouldn't reliably prove anything (EPP could just keep routing to the
other, healthy one). Both mocks always failed/scaled together in both tests.

```bash
# Manual check before writing the test:
curl -s -X POST http://localhost:18000/control -d '{"force_status": 500}'
curl -s -X POST http://localhost:18001/control -d '{"force_status": 500}'
curl -s -o /dev/null -w "status: %{http_code}, time: %{time_total}s\n" -X POST http://localhost:18080/v1/chat/completions ...
# Result: status: 500, time: 1.02s -- clean, fast, no hang, no gateway crash

kubectl --context kind-inference-poc -n inference-poc scale deployment vllm-mock-a vllm-mock-b --replicas=0
curl -s -X POST http://localhost:18080/v1/chat/completions ... --max-time 15
# Result: status 503 in 0.044s -- "inference error: ServiceUnavailable - failed to find endpoint
# candidates for serving the request". Even faster/cleaner than the 5xx case.
```

Writing the pod-scale-down test surfaced two real bugs in the test helper usage itself, both fixed:

```bash
# Bug 1: kubectl wait's own --timeout=30s and the Python subprocess wrapper's default timeout=30 can race
# -- subprocess.TimeoutExpired fired first and masked kubectl's own graceful exit. Fixed by passing a
# Python-level timeout comfortably larger than kubectl's own (--timeout=30s -> Python timeout=40;
# --timeout=60s -> Python timeout=70).
#
# Bug 2: kubectl wait --for=delete also exits 0 immediately if the selector already matches nothing at
# invocation time, AND a terminating pod briefly reports phase=Failed before the API server fully removes
# it -- neither a bare "wait succeeded" nor a single synchronous "get pods" snapshot right after robustly
# proves the pods are actually gone. Added an explicit poll (_wait_for_pod_phases) checking the real
# phase list rather than trusting either kubectl's exit code or one snapshot in time.
```

Also added explicit pod-health checks the user asked for specifically: a baseline check (both mocks
`Running`) before disrupting anything, and a stronger post-restore check (both mocks confirmed `Running`
again, not just `kubectl wait --for=condition=Ready` exiting 0) in the `finally` block.

```bash
pytest test_14_upstream_failure.py -v
# Result: 2 passed -- both the 5xx and pod-scale-down scenarios confirmed clean, fast failures with full
# recovery afterward
```

### Carry-forward items -- already covered, no new tests needed

Checked test_03_epp_routing.py and test_08_rls.py before writing anything new for these:
- EPP not-round-robin: test_03's `test_selection_flips_when_load_flips` already proves it -- flipping
  which mock reports lower load flips every single pick, which round-robin couldn't produce.
- RLS reserved-at-estimate/no-refund: test_08's `test_no_refund_mechanism_exists` already proves it
  structurally -- no Amend RPC exists at all, and `hits_addend` is unsigned so a negative value can't even
  be encoded.

### Full suite, zero regressions

```bash
pytest -v
# Result: 37 passed, 4 skipped (vLLM not running), 1 xfailed (invalid API key, known gap) -- 42 total,
# zero regressions across all 14 test files
```

Phase 8 done. Invalid API key remains an open, deliberate extended goal (same footing as KEDA) -- ask
again later, not decided against.
