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
