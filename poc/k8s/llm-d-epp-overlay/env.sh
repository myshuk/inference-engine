# Source this, then: kubectl kustomize poc/k8s/llm-d-epp-overlay | envsubst | kubectl apply -f -
export NAMESPACE=inference-poc
export EPP_NAME=vllm-epp
export POOL_NAME=vllm-mock-pool
export EPP_IMAGE=ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.10.0
export EPP_REPLICA_COUNT=1
# Pre-formatted YAML block-sequence fragment (see inference-pools.yaml's own comment
# in the upstream base) -- both mock replicas share this one containerPort.
export TARGET_PORTS=$'\n  - number: 8000'
export METRICS_ENDPOINT_AUTH=false
export ENABLE_LEADER_ELECTION=false
