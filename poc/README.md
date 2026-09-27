# Local POC: GPU Inference-as-a-Service Architecture (macOS, no NVIDIA GPU)

This folder is a local proof-of-concept validating the design in [`../wiki`](../wiki/Home.md) against a
real (if scaled-down) implementation — engine code, Kubernetes manifests, and everything else lands here as it's built, one phase at a time, as a learning exercise.

## Context

The machine running this POC is a MacBook Pro M4 Pro, 24GB RAM, Apple Silicon — **no NVIDIA GPU, no CUDA,
no NCCL/NVLink hardware**. Decisions already made:

- **Band 5 engine:** run **real vLLM** via [vllm-metal](https://github.com/vllm-project/vllm-metal) — a
  community-maintained plugin (hosted under the `vllm-project` GitHub org) that gives vLLM real Metal/MLX
  acceleration on Apple Silicon, instead of vLLM's generic CPU-only fallback (no Metal, ~20-30x slower).
  This is the actual Band 5 design decision (D1: adopt vLLM as the execution core), not a stand-in for it —
  and it comes with a real OpenAI-compatible `vllm serve` endpoint and real `vllm:*` Prometheus metrics for
  free, removing the need to hand-build either. The original minimal HF Transformers engine
  (`engine.py`/`run_cli.py`, distilgpt2, MPS-aware) is kept as a reference/fallback under
  `poc/engine/legacy/`, not deleted — vllm-metal is newer and community-maintained, so worth having a
  fallback if it hits a wall.
- **K8s platform:** `kind` — matches upstream Gateway API Inference Extension (GIE)'s own
  conformance-test environment, blank slate (no Traefik/klipper-lb to fight), trivial multi-node config.
- **Scope:** full breadth, thin at every layer — every band and control plane gets *something* running in
  pass one, including a minimal Kafka/ClickHouse/OpenMeter billing path and a stubbed model registry,
  rather than doing Bands 1-4 deep first.

**The one constraint that shapes everything below:** Docker containers on macOS run inside a Linux VM with
no Metal/MPS passthrough. This means the actual token-generation process (Band 5) **cannot run inside
`kind`** — it must run as a native macOS host process, with Kubernetes pointed at it from outside the
cluster. Band 6 (GPU Operator, NCCL, DCGM, Network Operator) has no real hardware to manage at all. Both
are addressed with explicit stand-ins below rather than skipped silently, so the POC still exercises the
scheduling/routing *logic* even though it can't exercise real GPU infrastructure.

## Acceptance criteria

This POC is "done" when all six of these hold — each is deliberately narrow and testable, not a vague
"the stack works":

1. **Native engine serves `/v1/chat/completions`, both streaming and non-streaming, with correct `usage`.**
   → Phase 1, tested directly against the real vLLM (vllm-metal) instance, no gateway in front yet.
2. **Official OpenAI Python SDK works against it.** → Phase 1, same target — proves real schema
   compatibility, not just "the JSON looks right."
3. ✅ **Two in-cluster mock servers demonstrate GIE/EPP selection using documented metrics/configuration.**
   → Phase 2, met. Deliberately *not* the real vLLM instances from Phase 1 — see the note in Phase 2 on why
   selection logic is validated against cheap, deterministic mocks instead of live engine load.
4. ✅ **agentgateway's custom-provider route preserves token accounting.** → Phase 2, met. Regression test:
   proved a naive `HTTPRoute → InferencePool` silently drops token counts (from agentgateway's own metering,
   not the client-visible JSON), then proved the documented wiring
   (`HTTPRoute → AgentgatewayBackend → custom provider → InferencePool`) doesn't.
5. **One versioned, idempotent usage event is captured end-to-end through a deliberately chosen collector
   boundary.** → Phase 6. "Deliberately chosen" because there's a real design decision here (agentgateway
   writing straight to Kafka vs. an OTel Collector sitting between agentgateway and Kafka) that shouldn't
   be left as an unexamined default — see Phase 6 for the options and the pick.
6. **Tests cover SSE framing, disconnect/drain behavior, invalid API key, rate-limit rejection, and
   upstream failure.** → Phase 8's test matrix, exercised against the full path once Phases 1-6 are wired.

## Phase 0 — Host prerequisites ✅ done

- **Podman** (not Docker Desktop/colima as originally considered) — fully open source, no licensing
  questions, kind's Podman provider is officially supported (`KIND_EXPERIMENTAL_PROVIDER=podman`) though
  still labeled experimental. Podman machine (the Linux VM Podman needs on macOS) bumped from its 2GiB
  default to **8GiB memory** before first use — too tight otherwise once Postgres/Kafka/ClickHouse/
  Prometheus/agentgateway are all running in it. `kubectl` and `helm` were already installed; `kind`
  installed via brew. `k9s` and `uv` both skipped deliberately — k9s is just a UI wrapper over the same
  `kubectl` calls, and the POC's Python dependency set is small enough that plain `venv`/`pip` covers it
  without uv's extra install.
- ⚠ **This machine runs Netskope** (corporate TLS-inspecting proxy) — breaks HTTPS image pulls from
  *inside* the Podman VM and from inside the `kind` node containers (each has its own separate trust
  store; fixing one doesn't fix the other). Root + intermediate CA certs saved at
  [`poc/extFiles/certs/`](extFiles/certs/); see `exeReadme.md` for the install-into-both-trust-stores fix
  if this resurfaces (e.g. after `kind` cluster recreation, or if Netskope rotates its CA again).
- The `host.docker.internal`-reachability question (Phase 0's original concern, for the "wire the real
  engine into the cluster" stretch goal) turned out not to matter yet — Phase 2 below uses in-cluster
  mocks instead of bridging to the host process, so this is deferred along with that stretch goal.

## Phase 1 — Band 5: real vLLM via vllm-metal ✅ done (criteria 1 & 2 met)

1. Installed vllm-metal (needs **native arm64 Python 3.12** — Rosetta/x86_64 Python is not supported).
   **Correction from the original plan:** did *not* end up project-local. The installer only puts the venv
   at `$PWD/.venv-vllm-metal` when run from a full git clone (needs Xcode's Metal toolchain to build native
   kernels from source); piped/standalone it always uses the prebuilt-wheel path at the default
   `~/.venv-vllm-metal` — took that path deliberately (simpler, no toolchain dependency, more battle-tested)
   rather than fighting for a project-local path. Downloaded the installer script to
   [`poc/extFiles/vllm-metal-install.sh`](extFiles/vllm-metal-install.sh) and ran it from there instead of
   piping `curl | bash` directly, so there's a durable record of exactly what ran.
2. Model: **Qwen2.5-1.5B-Instruct**, as planned.
3. Served with `vllm serve Qwen/Qwen2.5-1.5B-Instruct --port 8001`. Confirmed real Metal/MLX acceleration
   (not the CPU fallback) via the "Platform plugin metal is activated" log line. Verified, against the
   running server directly (no gateway in front yet):
   - non-streaming `/v1/chat/completions` — valid response, correct `usage`.
   - streaming — correct SSE framing (`data: {...}` chunks, `[DONE]` terminator). Streaming `usage`
     specifically needs `stream_options: {"include_usage": true}` (OpenAI protocol requirement, easy to
     miss) — confirmed vLLM emits a final `usage`-only chunk before `[DONE]` when it's set.
   - the **official `openai` Python SDK** (`poc/engine/test_client.py`), not just raw `curl` — both
     streaming and non-streaming, asserting `usage` math is internally consistent.
4. **Not done, and no longer part of the current plan:** the original step 4 here ("run two instances on
   8001/8002 for the Endpoint Picker to pick across") is superseded by the acceptance-criteria redesign —
   criterion 3 explicitly validates EPP selection against **in-cluster mocks**, not real engine instances
   (see Phase 2's "why mocks" note). Running a second real vLLM instance is now only relevant for the
   optional stretch goal of wiring the real engine into the full gateway path later.
5. Sanity-check done implicitly via the functional tests above rather than as a separate benchmarking
   pass — no throughput/latency numbers captured, but responses returned promptly with no signs of the
   engine being unusably slow.

## Phase 2 — Bands 2-4: `kind` + Gateway API + agentgateway + GIE (criteria 3 & 4 met)

1. ✅ `kind create cluster`, 2-node (control-plane + worker) via `poc/k8s/kind-config.yaml`.
2. ✅ Installed Gateway API CRDs (`kubernetes-sigs/gateway-api` v1.5.1) and GIE's `InferencePool` CRD
   (`kubernetes-sigs/gateway-api-inference-extension` v1.5.0), as planned.
   **Correction — significant, discovered mid-implementation:** `InferenceObjective` did *not* come from
   there. Partway through this project, GIE split — the Endpoint Picker (EPP) itself, plus
   `InferenceObjective` and `InferenceModelRewrite`, moved out to a new repo, **`llm-d/llm-d-router`**
   (new API group `llm-d.ai`, not `inference.networking.k8s.io`). The original GIE repo now only keeps
   `InferencePool` and a stripped-down reference EPP for conformance testing. Chose to use the real
   `llm-d-router` EPP over that reference version — see step 4. (Wiki's Band 4 page has since been updated
   to reflect this split.)
3. ✅ Deployed **agentgateway** — not via a bare `helm upgrade -i` as originally written, but as two local
   umbrella charts (`poc/k8s/agentgateway-crds-lab/`, `poc/k8s/agentgateway-lab/`), each a `Chart.yaml`
   pinning one upstream OCI chart as a dependency + an explicit `values.yaml` override, resolved via
   `helm dependency update` (pattern borrowed from an existing project, `explore/ceph/helm/ceph-lab`) —
   gives a durable, versioned local record of exactly what's installed instead of an implicit registry
   pull. `GatewayClass agentgateway` confirmed `ACCEPTED: True`.
4. ✅ **Criterion 3 met.** `InferencePool` + Endpoint Picker are up; EPP comes from `llm-d-router` (not
   bundled with GIE — see step 2's correction):
   - `llm-d-router`'s own ready-made deploy overlays only target Istio and kgateway, not agentgateway — so
     `poc/k8s/llm-d-epp-overlay/` is our own Kustomize overlay, patching their generic
     `deploy/components/inference-gateway` base to set `gatewayClassName: agentgateway` instead.
   - EPP image pinned to `ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.10.0` (a real release tag, not
     their Makefile's `dev` default). Scoring config (`poc/k8s/llm-d-epp-overlay/epp-configmap.yaml`) adds
     `queue-scorer` + `kv-cache-utilization-scorer` (weight 2 each) alongside the sample's
     `prefix-cache-scorer` (weight 1) — the sample's plugins alone don't read any load metric at all
     (confirmed against llm-d-router's own `docs/plugin-metric-protocol.md`), so this was necessary to have
     anything controllable to test selection against. Real metric name is `vllm:kv_cache_usage_perc` — the
     wiki never actually specified a metric name (an earlier draft of these POC docs wrongly claimed it
     said `vllm:gpu_cache_usage_perc`); wiki has now been updated with the real names, see below.
   - **Two mock servers built** (`poc/k8s/mock-servers/`) — stdlib-only Python (`http.server`, no custom
     image needed), labeled `app=vllm-mock-pool`, serving `/health`, `/metrics`
     (`vllm:num_requests_waiting`, `vllm:kv_cache_usage_perc`), a `/control` endpoint to set those live, and
     `/v1/chat/completions` (streaming + non-streaming, response content identifies the answering pod).
     `vllm-mock-a` starts low-load, `vllm-mock-b` starts high-load.
   - **Verified end-to-end through the real gateway path**: 5/5 requests routed to `vllm-mock-a` (low
     load); flipped both mocks' load via `/control`, reran, 5/5 routed to `vllm-mock-b` instead — EPP's pick
     tracks the metrics, not sticky or random. See exeReadme.md for the two real bugs this surfaced and
     fixed along the way (a missing agentgateway RBAC grant, and — the actual root cause — agentgateway's
     `InferencePool` support being a feature flag defaulted off, `inferenceExtension.enabled` in
     `poc/k8s/agentgateway-lab/values.yaml`, undocumented as a hard requirement anywhere obvious upstream).
     For the exact scoring formulas and a worked-through example of why this result is deterministic, see
     [`epp_scoring.md`](epp_scoring.md).
5. ✅ **Built.** `poc/k8s/mock-servers/agentgateway-backend.yaml` — an `AgentgatewayBackend` with
   `spec.ai.provider.custom.backendRef` targeting the `InferencePool` directly (schema confirmed against
   agentgateway's own Go types: a custom LLM provider's `backendRef` may target a `Service` or
   `InferencePool`). `poc/k8s/llm-d-epp-overlay/patch-httproute-backend.yaml` repoints the `HTTPRoute` at
   this `AgentgatewayBackend` instead of the bare `InferencePool` — this is the corrected
   `HTTPRoute → AgentgatewayBackend → custom provider → InferencePool` chain, confirmed working (step 4's
   test above runs through it). Also had to bump agentgateway from `v1.4.1` to `v1.5.0-beta.1` along the
   way (temporary, deliberate — `v1.4.1` predates an upstream fix for this exact provider/`InferencePool`
   combination; no stable release has the fix yet).
6. ✅ **Criterion 4 met.** With `inferenceExtension.enabled` fixed, the naive `HTTPRoute → InferencePool`
   backendRef now actually resolves and routes (`ResolvedRefs: True`) — unlike before, it's not broken at
   the transport level anymore, which made the actual token-accounting claim testable for the first time.
   Swapped the same `HTTPRoute` object's `backendRefs` between the two configurations and compared
   agentgateway's own `agentgateway_gen_ai_client_token_usage` Prometheus metric (data-plane pod, `:15020`)
   before/after an identical request each time:
   - **(a) naive `HTTPRoute → InferencePool`:** request succeeded (client got correct `usage` in the raw
     JSON body, from the mock itself), but the metric's `count`/`sum` were **byte-for-byte unchanged**
     afterward — agentgateway never parsed the body, so it has nothing to meter.
   - **(b) `HTTPRoute → AgentgatewayBackend → custom provider → InferencePool`:** same request shape,
     `count` incremented (`1→2`) and `sum` increased by exactly the response's real token counts
     (output `+4`, input `+2`), correctly labeled by `route`.
   - Also noticed (a)'s response used a different `model` field value (`mock-model`, the mock's own
     default) than what was sent (`mock`) — the naive path may be mutating/dropping the request body's
     `model` field somewhere (possibly GIE's own model-based header routing convention). Not investigated
     further; flagging in case it matters later.
   - Order tested was (b) baseline → (a) request (metric unchanged) → back to (b) (metric incremented
     again) — (b) working both before and after (a)'s no-op rules out an ordering fluke.
7. Not yet actively addressed, but nothing done so far contradicts it — no Cloudflare/MetalLB has been
   stood up; `kind`'s own port mapping remains the plan for local ingress.

## Phase 2.5 — Control Plane B: human signup via Keycloak (added mid-project, no SSO) ✅ done

Not part of the original 6 acceptance criteria or the Phase 0-8 sequence — added after a real gap surfaced
while discussing Phase 2's token-accounting proof: nothing in the design so far actually *creates* a user
identity. The wiki's own Control Plane B page defers Keycloak "until the first enterprise SSO ask," and
even its own MVP note ("email + password, behind a thin abstraction") was never built. Scoped narrowly to
just that MVP note — human signup only, no SSO/IdP federation — not the full self-serve portal (API key
issuance, key rotation, usage dashboards), which stays deferred.

1. ✅ Deployed via `poc/k8s/keycloak-lab/` (same umbrella-chart pattern as agentgateway) — pins
   `codecentric/keycloakx` v7.3.1, chosen over `bitnami/keycloak` because Bitnami gated most of its image
   tags behind a paid tier in 2025; codecentric uses the official `quay.io/keycloak/keycloak` image
   directly. `values.yaml` runs it in dev mode (`start-dev`, built-in in-memory H2 database) with fixed
   bootstrap admin credentials for the POC.
   ⚠ **Confirmed the hard way:** dev-mode's H2 database is genuinely ephemeral — a pod restart (from
   pausing/resuming the whole environment) wiped it completely, including the realm created below. Had to
   recreate the realm from scratch on resume. Fine for proving the signup flow works; would need a real
   database + `start` (not `start-dev`) for anything meant to survive restarts.
2. ✅ Created a `developers` realm via the admin REST API (not `master`, which is reserved for Keycloak's
   own admin) with `registrationAllowed: true`, `registrationEmailAsUsername: true` (no separate username
   field), `verifyEmail: false` (no SMTP configured in this POC).
3. ✅ **Verified via the actual public self-registration form, not the admin API** — deliberately, since
   admin-created users skip CSRF and the real signup code path entirely, which would prove the API works
   but not that a human's browser flow does. Drove it with `curl` + a cookie jar through the
   `account-console` client's OIDC flow (which enforces PKCE — had to generate a `code_verifier`/`S256`
   challenge), followed the registration link, parsed the form's `session_code`/`execution` values out of
   the returned HTML, and submitted real signup data. Result: `302` redirect back into the account console
   carrying a real authorization `code`, and the new user (`test.developer@example.com`) confirmed present
   via the admin API afterward — enabled, no pending required actions.

## Phase 3 — Band 3: identity, tenancy, counters (real, in-cluster) ✅ done

This band is the most directly reproducible as designed — nothing here depends on a GPU. Deployed into
its own `identity-tenancy` namespace; manifests live per-component under `poc/k8s/postgres/`,
`poc/k8s/valkey/`, `poc/k8s/rls/` (not a single `band3/` folder — component name, not phase number, same
correction applied to the namespace itself).

1. ✅ **Postgres** (single `Deployment` + PVC, not `CloudNativePG` — the plan's own simpler option).
   `poc/k8s/postgres/schema.sql` implements orgs → projects → plans → hashed `api_keys` →
   `model_allowlist`, mounted at `/docker-entrypoint-initdb.d` so the official image auto-runs it on first
   init. Seeded with one org/project/key/plan/allowed-model row for testing. User/db were initially named
   `band3` (same naming slip as the folder), renamed to `identity_tenancy` — required a fresh PVC since
   `POSTGRES_USER`/`POSTGRES_DB` only apply on first init of an empty data directory. Verified via a join
   query across all five tables returning the seeded row correctly.
2. ✅ **Valkey** (`Deployment`, no persistence — lossy-by-design per the wiki, not the billing source of
   truth). Verified with a basic `set`/`get`.
3. ✅ **`envoyproxy/ratelimit` (RLS)**, pointed at Valkey via the generic `REDIS_URL` env var (RLS doesn't
   care which server implements the Redis wire protocol — same reason Valkey works as a drop-in).
   ⚠ **Image staleness, a real finding:** `envoyproxy/ratelimit`'s Docker Hub hasn't been pushed to since
   March 2021 — confirmed via their own GitHub Actions workflows (only Docker Hub is targeted, on `v*` tags
   or `main` merges; no new `v*` tag since 2020, no GHCR mirror). Pinned to `c03723f3` (the last image
   actually published), amd64-only (ran fine on this Apple Silicon Mac via Podman's emulation) — accepted
   deliberately since the core check-and-increment algorithm this POC needs is small and stable. Domain
   config (`poc/k8s/rls/config.yaml`) defines `api_key_rpm`/`api_key_tpm` descriptors matching the seeded
   "free" plan's limits (60 rpm / 10000 tpm) — logs confirmed both loaded correctly and a live Valkey
   connection pool was established.
4. ✅ **Verified the actual `ShouldRateLimit` gRPC call directly** (not yet wired to agentgateway — see
   below), using `grpcurl` against a minimal hand-written `.proto` (the real one pulls in Envoy's full
   `validate`/`udpa` annotation dependency tree, unnecessary for wire-level testing; field numbers kept
   identical to the real proto). Confirmed:
   - RPM check-and-increment: `hits_addend: 1` against the seeded key → `limitRemaining: 59` (60 − 1).
   - TPM check-and-increment with a variable cost (the "estimate" half of D11): `hits_addend: 500` →
     `limitRemaining: 9500` (10000 − 500).
   - **D11's "amend can't refund" claim, confirmed at the protocol level, not just by reading docs:**
     attempted `hits_addend: -450` to give back the unused estimate — rejected outright, because the field
     is `uint32` and a negative value can't even be encoded. Combined with `RateLimitService` having
     exactly one RPC (`ShouldRateLimit` — no `Amend`/`Refund` method exists anywhere in the service
     definition), there is no mechanism to decrement this counter, structurally, not just by convention.
   - **A related, non-obvious gotcha found along the way:** a follow-up `hits_addend: 0` call (intended as
     a zero-cost "peek") actually cost 1 hit — in proto3 JSON, an explicit `0` on a scalar field is
     indistinguishable from the field being unset, and RLS's documented fallback for "not set" is to
     increment by 1. Worth knowing for whoever wires the real CEL policy: there's no reliable zero-cost
     check via this interface.
   - Also directly observed the fixed-window reset in action: enough real time passed between the 500-hit
     estimate and the follow-up peek that a new 1-minute window began, refilling the counter back toward
     10000 before the peek's implicit +1 — a live instance of the wiki's own "overshoot is bounded only by
     window refill" language.
5. ✅ **Wired agentgateway's real traffic-policy stage to call RLS.** `poc/k8s/rls/agentgateway-policy.yaml`
   (an `AgentgatewayPolicy` targeting the `HTTPRoute`) + `referencegrant.yaml` (cross-namespace: the policy
   lives in `inference-poc`, RLS lives in `identity-tenancy`). Same domain/descriptor keys as the RLS config
   already built — this is the same rate limiting already proven via `grpcurl`, now on the live request path.
   - **Option chosen: agentgateway's native pattern, not a manual D11-style reconstruction.** Two
     descriptors: `api_key_rpm` (`unit: Requests`, cost defaults to `1`, checked pre-dispatch) and
     `api_key_tpm` (`unit: Tokens`, cost defaults to the real total token count, charged once after the
     response completes). No API-key authentication is wired yet (Band 3's separate
     `APIKeyAuthentication` policy) — the descriptor value comes directly from an `x-api-key` request
     header for now, matching the seeded plaintext key.
   - **Real finding, more sophisticated than the CRD docs suggested:** tracing `crates/agentgateway/src`
     (source, not just the Go CRD type comments) revealed agentgateway's `Tokens` unit actually makes
     **two** real calls against the same descriptor — a pre-dispatch "estimate" (cost `0` by default, no
     tokenizer configured) and a post-completion "amend" that sends the real total as a *delta*
     (`resp_input − req_input + output_tokens`), specifically designed to avoid double-counting. This is
     closer to D11's real intent than the doc comment implied.
   - ⚠ **A real scare, resolved:** initial testing showed both calls charging a flat `1` regardless of
     actual token count (confirmed across multiple requests with very different real usage — not a
     one-off). Traced deep into agentgateway's Rust source (`llm/mod.rs`, `types/completions.rs`) looking
     for a parsing bug in the `custom` provider path; the code read correctly for our exact response
     shape at every layer checked. Got live `trace`-level logs from the actual data-plane binary by
     attaching an `AgentgatewayParameters` object via the `GatewayClass`'s `parametersRef` (patching the
     managed `Deployment` directly doesn't work — the controller reconciles it back within seconds; the
     `GatewayClass` isn't continuously reconciled the same way, so this sticks). The trace logs showed
     `hits_addend=Some(<real total>)` — **correct** — on a freshly-restarted pod. **Root cause: not a
     code or config bug at all** — the original data-plane pod (up 8+ days through dozens of config
     changes this session) had accumulated some stale internal state; a restart alone fixed it, with zero
     other changes. Debug logging removed afterward (trace-level isn't left running normally).
   - ✅ **Verified with an automated test**, not just manual curl+log inspection —
     `poc/tests/test_09_rate_limit_wiring.py`: sends one real request through the gateway with a fresh,
     never-seen API key, then confirms via a direct RLS query that the counter depleted by exactly the
     response's real `total_tokens`, not a flat cost. Accounts for the amend call being fire-and-forget
     async (spawned, not awaited before the HTTP response returns) by polling briefly rather than
     assuming it's already landed.

## Phase 4 — Band 6: GPU infrastructure (simulated, not real) ✅ done

Nothing here can run for real — no NVIDIA driver, no NVLink, no RDMA fabric. Thin stand-ins that still
exercise the *scheduling logic*:

1. ✅ **Kueue** — installed for real via `poc/k8s/kueue-lab/` (umbrella chart, same pattern as
   agentgateway/Keycloak — official chart at `oci://registry.k8s.io/kueue/charts`, pinned `0.19.4`, current
   and actively maintained, no staleness concerns like RLS). It's GPU-agnostic; gang-scheduling works
   against any quota-backed resource, so it installs and runs completely normally with zero GPU hardware.
   Clean startup confirmed via logs — all 11 CRDs installed, internal cert management (no cert-manager
   dependency needed) working, no errors.
2. ✅ **Fake GPU resource** — patched `inference-poc-worker`'s node `.status.capacity`/`.status.allocatable`
   directly via `kubectl patch --subresource=status` to advertise a fabricated `nvidia.com/gpu: 2`. Wired
   up the real Kueue object chain against it (`poc/k8s/kueue-lab/fake-gpu-queue.yaml`): a `ResourceFlavor`
   (our one fake-hardware category), a `ClusterQueue` (the actual quota: 2 GPUs, matching the fabricated
   capacity), and a `LocalQueue` in a new `gpu-jobs` namespace (the namespace-scoped front door jobs
   actually submit against). Used the current `kueue.x-k8s.io/v1beta2` API, not the deprecated `v1beta1`
   the docs default to.
   - **Verified real quota enforcement, not just that the objects report Ready** —
     `poc/tests/test_10_kueue.py`: submits 3 plain `batch/job` Jobs (each requesting 1 fake GPU, labeled
     `kueue.x-k8s.io/queue-name`) against the 2-GPU quota, confirms Kueue's own controller admits exactly 2
     (flips `spec.suspend` to `false`) and holds the 3rd suspended — real gang-scheduling/admission logic,
     no GPU hardware underneath. Polls briefly since admission is async; uses a unique job-name suffix per
     run so a leftover job from an interrupted run can't eat into a later run's quota.
   - **DRA (Dynamic Resource Allocation) explicitly not covered here, by design** — it's a different
     Kubernetes resource model entirely (`resource.k8s.io` API: `DeviceClass`/`ResourceClaim`/`ResourceSlice`,
     not the classic `.status.capacity` extended-resource model this phase uses) and needs a real or
     simulated DRA *driver* publishing device inventory, not a simple node patch. Confirmed our cluster
     already has the DRA API available (`resource.k8s.io/v1`, no feature-gate changes needed) and that
     Kueue's own DRA integration is at beta as of `v0.19` (the version installed here) — so this would be a
     real, valid extension if picked up later (`kubernetes-sigs/dra-example-driver` exists specifically for
     hardware-free DRA testing), just deliberately out of scope for this pass.
3. **GPU Operator, NCCL, DCGM, Network Operator** — cannot be installed (they require real NVIDIA
   drivers/hardware). Documented as explicitly out of scope for this POC rather than attempting a fake
   install.

## Phase 5 — Control Plane D: observability (Prometheus + Grafana done; KEDA deferred, stretch items deferred)

1. ✅ **`kube-prometheus-stack`** (Prometheus + Grafana only, scoped per explicit decision — KEDA deferred to
   end of POC, item 3 below deferred, ask again later) — `poc/k8s/prometheus-lab/` (umbrella chart, same
   pattern as agentgateway/Keycloak/Kueue, pinned `91.4.1`). Alertmanager disabled (no alerting use case
   yet); `kubeControllerManager`/`kubeScheduler`/`kubeEtcd`/`kubeProxy` disabled (on `kind` these run as
   static pods bound to `127.0.0.1` only, not reachable by ServiceMonitor-based scraping — a well-known
   `kind` limitation, not a config bug); `kubeApiServer`/`kubelet`/`coreDns` left enabled since those *are*
   reachable.
   - **Real PVC added** (`storageSpec.volumeClaimTemplate`, 2Gi) — by default `Prometheus.spec.storage` is
     unset, so the TSDB lives on ephemeral storage tied to the pod's lifecycle; the `retention: 10d` setting
     is meaningless without this, since a pod restart (which happens on every `podman machine` pause/resume
     in this project) would otherwise wipe all history. Confirmed `kind`'s `standard`/`rancher.io/local-path`
     StorageClass already works (same as Postgres's own PVC) before adding this.
   - **Two ServiceMonitors + a PodMonitor's replacement, not zero-config discovery** — left the Operator's
     default `serviceMonitorSelectorNilUsesHelmValues: true` alone (declined to loosen cluster-wide
     discovery) and labeled our own objects with `release: prometheus` instead, matching the Helm release.
   - **Bug found and fixed: `vllm-mock-pool`'s ServiceMonitor matched zero targets.**
     `poc/k8s/mock-servers/service.yaml`'s Service had no `metadata.labels` of its own — only
     `spec.selector` (a different field, used for pod-matching). `ServiceMonitor.spec.selector` matches a
     Service's own `metadata.labels`. Fixed by adding the missing label.
   - **RLS has no `/metrics` endpoint at all** (confirmed `404`; only `/healthcheck` exists — `USE_STATSD`
     was `false`, so its `gostats` metrics only ever went to stdout logs). Fixed by enabling
     `USE_STATSD=true` and adding a `prom/statsd-exporter` sidecar (`poc/k8s/rls/statsd-exporter.yaml`) that
     RLS's `gostats` client pushes to over statsd/**TCP** (its default transport, not UDP — the exporter
     listens on both on the same port). Exposes a normal, unbroken Prometheus `/metrics` endpoint.
   - **Real upstream bug found and worked around: agentgateway's `/metrics` rejects the entire scrape.**
     agentgateway (`v1.5.0-beta.1`, confirmed still present on `main`) always encodes its metrics body with
     the OpenMetrics-only encoder from its `prometheus_client` Rust dependency, but hardcodes the response
     `Content-Type` header to classic `text/plain;charset=utf-8` whenever protobuf isn't requested.
     OpenMetrics-only constructs (the `info` type, used by `agentgateway_build_info`) aren't legal under
     that header's implied format, so Prometheus rejects the *entire* scrape — zero metrics, not just the
     one bad line, including `agentgateway_gen_ai_client_token_usage` (the metric that proved criterion 4 in
     Phase 2). Confirmed by reading `crates/agentgateway/src/management/metrics_server.rs` directly;
     confirmed this isn't fixable from the Prometheus side (`PodMonitor.spec.fallbackScrapeProtocol` and
     `scrapeProtocols` both tested empirically and don't apply — they only activate when Content-Type is
     missing/unrecognized, not when it's valid-but-semantically-wrong, as it is here). No existing upstream
     issue found. Worked around with `poc/k8s/agentgateway-metrics-relay/` — a small standalone relay
     (can't patch a sidecar into agentgateway's own managed pod; the GatewayClass controller reverts that
     within seconds) that fetches `:15020/metrics` verbatim and re-serves the identical bytes under the
     corrected `application/openmetrics-text` Content-Type, which Prometheus's OpenMetrics parser already
     fully supports.
   - **Verified end-to-end, not just "pod is Running"** — `poc/tests/test_11_metrics.py`: confirms all three
     new targets show `up` via Prometheus's own `/api/v1/targets`, and that real (non-placeholder) data is
     queryable for each — the mocks' `vllm:kv_cache_usage_perc`, a live request's
     `agentgateway_gen_ai_client_token_usage_sum` through the relay, and RLS's
     `ratelimit_service_config_load_success` through statsd-exporter — plus that Prometheus's PVC is
     actually `Bound`.
   - **Loki/Tempo/OpenTelemetry tracing** (the wiki's own Control Plane D page mentions these; the original
     plan above never itemized them) — skipped for now along with KEDA, per the same "ask again later"
     decision; logs stay in `kubectl logs`/stdout as they have been throughout this POC.
2. **KEDA** — deferred to the end of the POC (explicit decision). Scope note carried forward: since Band 5
   replicas are host processes, not Pods, KEDA can't actually scale them — point it at a dummy in-cluster
   `Deployment` to prove the `ScaledObject` mechanics instead.
3. **Argo CD, OpenBao (dev-mode single container), Trivy (CLI scan step), Falco** — deferred, not decided
   against; ask again later. They don't affect whether a request flows correctly end-to-end. Falco remains a
   probable skip regardless: likely to fight `kind`-on-Docker-Desktop's Linux VM eBPF/kernel compatibility.

## Phase 6 — Control Plane A: billing (thin, shared infra as documented)

Per the wiki's own principle (one Kafka topic, one ClickHouse cluster, not one pair per consumer):

1. Single-node Kafka in KRaft mode (no ZooKeeper).
2. Single-node ClickHouse.
3. OpenMeter, self-hosted, configured to consume from the **same** Kafka topic/ClickHouse cluster rather
   than its own bundled dev pair.
4. Stripe in **test mode** (sandbox keys, no real charges) for the final rating→payment hop.
5. **The collector-boundary decision (criterion 5).** agentgateway already emits OTel token-usage data
   (Band 4's backend-policy stage) — the open question is *where that becomes a durable, billable event*.
   Two candidates:
   - **(a) agentgateway writes straight to the Kafka topic.** Fewer moving parts, but couples agentgateway
     directly to Kafka's client/schema, and any batching/transform/retry logic has to live inside
     agentgateway itself.
   - **(b) an OpenTelemetry Collector sits between agentgateway and Kafka** (agentgateway → OTel Collector
     → Kafka). agentgateway just emits OTel, same as its tracing path; the Collector owns
     batching/transform/export and is where the versioning below actually gets stamped onto the event.
     **Recommended** — it's the standard shape for "telemetry in, durable record out" and keeps
     agentgateway's job (decide + emit) separate from the collector's job (transform + guarantee delivery).
   - Pick one deliberately and document it here once built, rather than defaulting to (a) by inertia.
6. **Event schema: versioned + idempotent.** Each usage event carries `event_version` (so the schema can
   change without breaking old consumers) and `event_id` — derived from agentgateway's own per-request ID,
   not freshly generated at the collector boundary — so a redelivered event is a no-op. Idempotency is
   enforced at the ClickHouse/OpenMeter consumer side (dedupe on `event_id`, e.g. `ReplacingMergeTree`),
   not by trying to guarantee exactly-once delivery upstream.

## Phase 7 — Control Plane C: model registry (stubbed, symbolic)

Since the POC pins one fixed HF model pulled directly by the engine, the full pipeline is overkill — thin
stand-in that still demonstrates the three-stage shape:

1. **Entry:** skip the quantisation pipeline itself; note where it would sit.
2. **Storage:** SeaweedFS single-binary (Apache 2.0, matches the wiki's anti-MinIO licensing stance) to
   hold a copy of the model weights, standing in for Ceph/SeaweedFS at rest.
3. **Delivery:** a plain local OCI registry (`registry:2`) standing in for Harbor (no signing enforcement
   at POC scale). Not on Band 5's actual startup path — symbolic only.

## Phase 8 — Acceptance validation: the criterion-6 test matrix

By this point criteria 1-5 each have a specific test living in the phase that built them (Phase 1, Phase 2
x2, Phase 6). Phase 8 is criterion 6 specifically — a test matrix run against the full assembled path
(Cloudflare-stand-in skipped locally → agentgateway → GIE → mocks or real engine → Band 3):

| Scenario | What it proves | Where it touches |
|---|---|---|
| **SSE framing** | Chunks are well-formed `data: ...` events with a correct terminator, not just "streaming looks right" eyeballed in a terminal | Band 1 contract, agentgateway frontend policies |
| **Disconnect / drain** | A client disconnecting mid-stream, or the server side draining a connection, doesn't leave a hung request or a truncated write | Non-negotiable constraint #1; ties to Phase 5's KEDA grace-period note |
| **Invalid API key** | Rejected with the right status/error shape before it ever reaches GIE, and Postgres/Valkey remain the source of truth for "is this key real" | Band 3 |
| **Rate-limit rejection** | RLS actually rejects once the bucket is exhausted, and `failureMode` behaves as configured (`failOpen` for rate limits) | Band 3, D11 |
| **Upstream failure** | A mock/engine returning 5xx or hanging doesn't take the whole gateway down, and surfaces as a clean error to the client | agentgateway backend policies, GIE |

Also carry forward from the original plan (not a named criterion, but still worth confirming once wired):
- Concurrent requests against the Phase 2 mocks show the Endpoint Picker actually choosing based on
  the controllable metrics (not round-robin) — the concrete counterfactual the wiki's D3 action item calls
  for.
- The RLS/Valkey counter in Band 3 behaves per D11 (reserved at estimate, never decremented by amend).

## Working style

Step by step, as a learning exercise. Work through the phases above one at a time in order (Phase 0 → 1 →
2 → …). At each phase: implement it, explain what was just built and why (not just that it works), verify
it before moving on, and pause for questions rather than batching multiple phases into one pass.

## Automated test suite (`poc/tests/`)

Everything verified manually via `curl`/`grpcurl`/port-forward throughout this project (see
`exeReadme.md`) is also captured as a real, rerunnable `pytest` suite — one file per phase/component
(`test_01_vllm.py` … `test_11_metrics.py`), a shared `conftest.py` managing each service's port-forward
lifecycle (starts once per session, blocks until the local port actually accepts connections, tears down
at the end), and `helpers.py` for the `kubectl` wrapper. Each new component gets a new `test_NN_*.py` file
here, and the whole suite gets rerun after any change — not just the newest piece — to catch regressions
in things that were already working.

```bash
cd poc/tests
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # brew install grpcurl separately -- not a pip package
pytest -v
```

Design notes:
- **Session-level pre/post cleanup** (`pytest_sessionstart`/`pytest_sessionfinish` in `conftest.py`) kills
  any leftover `kubectl --context kind-inference-poc ... port-forward` processes before and after every
  run — a backstop beyond each fixture's own teardown, covering a crashed prior run or a manual debugging
  session that left something behind. Scoped specifically to this project's context, not a blanket
  `pkill port-forward` that could hit unrelated processes.
- **Not vendored port numbers made up per-test** — the same local ports used throughout this README/exeReadme
  (`18080` gateway, `18000`/`18001` mocks, `8180` Keycloak, `15020` agentgateway stats, `8081` RLS gRPC) so
  manual `curl` poking and the automated suite never conflict or confuse which port means what.
- **Tests that mutate shared state restore it**, regardless of pass/fail — e.g. `test_04`'s
  `restore_corrected_route` fixture always puts the `HTTPRoute` back on the `AgentgatewayBackend` wiring
  even if the naive-path test fails partway through, since every other test (and manual use of the
  cluster) assumes that resting state.
- **Tests use unique, randomly-suffixed identifiers** where re-running against accumulated state would
  give a false result — RLS descriptor values, Keycloak signup emails — rather than fixed test data that
  would only work once per rate-limit window or realm lifetime.
- **`test_01_vllm.py` skips cleanly** (doesn't fail) if the native `vllm serve` process isn't running,
  since that's a host process this suite doesn't start on your behalf.
- **The RLS gRPC proto is hand-minimized** (`protos/rls-minimal.proto`) — the real
  `envoy.service.ratelimit.v3` proto pulls in Envoy's full `validate`/`udpa` annotation dependency tree,
  which doesn't affect wire compatibility; field numbers are kept identical to the real proto.
- Three real bugs were caught building this out (worth knowing before extending it further): EPP's own
  ~5s Prometheus re-export interval means a `/control` change needs a settle delay before asserting on
  routing; a Prometheus histogram's `_count` (number of observations) is not the same as `_sum` (actual
  token totals) — comparing token counts against `_count` always looked like "off by the exact wrong
  amount"; and Keycloak marks its session cookies `Secure`, so Python's `requests` (correctly, per RFC
  6265) won't resend them over plain `http://` the way `curl` did in earlier manual testing — cookies
  need to be attached explicitly via a `Cookie` header instead of relying on `Session`'s automatic jar.
