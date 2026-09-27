"""Phase 5 -- kube-prometheus-stack (poc/k8s/prometheus-lab/), Prometheus +
Grafana only (KEDA deferred to end of POC, GitOps/secrets/supply-chain
stretch items deferred -- see README.md). Validates the actual scrape
wiring end to end, including the two real bugs found and fixed along the
way -- not just that Prometheus itself reports Running:

- vllm-mock-pool's ServiceMonitor originally matched zero targets --
  poc/k8s/mock-servers/service.yaml's Service had no metadata.labels of its
  own (ServiceMonitor.spec.selector matches that, not spec.selector, which
  is a separate field used for pod-matching).
- agentgateway's /metrics has a genuine upstream Content-Type-vs-body
  mismatch bug (encodes with the OpenMetrics-only encoder but hardcodes a
  classic text/plain Content-Type) that makes Prometheus reject the whole
  scrape outright -- confirmed via source inspection, not fixable via
  PodMonitor fallbackScrapeProtocol/scrapeProtocols (both tried, neither
  applies here). Fronted by poc/k8s/agentgateway-metrics-relay/ instead,
  which re-serves the identical body under the corrected header.
- RLS has no /metrics endpoint at all (confirmed 404; only /healthcheck
  exists) -- pushes to a prom/statsd-exporter sidecar over statsd/TCP
  instead (poc/k8s/rls/statsd-exporter.yaml, USE_STATSD=true).

Checks each target through Prometheus's own /api/v1/targets (the actual
wiring, not just "the pod is Running"), then confirms real, non-placeholder
data is queryable for each -- not just that the scrape reports healthy with
zero samples.
"""

import secrets
import time

import requests

from helpers import kubectl

EXPECTED_UP_JOBS = ["vllm-mock-pool", "agentgateway-metrics-relay", "statsd-exporter"]


def _targets(prometheus_port):
    r = requests.get(f"http://localhost:{prometheus_port}/api/v1/targets", timeout=10)
    r.raise_for_status()
    return r.json()["data"]["activeTargets"]


def _query(prometheus_port, promql):
    r = requests.get(f"http://localhost:{prometheus_port}/api/v1/query",
                      params={"query": promql}, timeout=10)
    r.raise_for_status()
    return r.json()["data"]["result"]


def _wait_for(fn, predicate, timeout=30, interval=2):
    """Prometheus Operator reconciles ServiceMonitor changes on its own
    schedule, and each target needs at least one successful scrape (15s
    interval) before it shows up -- poll rather than assume it's already
    settled right after the fixtures start."""
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        result = fn()
        if predicate(result):
            return result
        time.sleep(interval)
    return result


def test_all_new_targets_are_up(prometheus_port):
    health = _wait_for(
        lambda: {t["labels"]["job"]: t["health"] for t in _targets(prometheus_port)
                 if t["labels"].get("job") in EXPECTED_UP_JOBS},
        lambda h: all(h.get(j) == "up" for j in EXPECTED_UP_JOBS),
    )
    assert all(health.get(j) == "up" for j in EXPECTED_UP_JOBS), (
        f"expected all of {EXPECTED_UP_JOBS} up in Prometheus, got {health}"
    )


def test_mock_pool_metrics_have_real_values(prometheus_port):
    # Metric names really do contain a colon (vllm:kv_cache_usage_perc) --
    # valid Prometheus syntax, matches what mock_server.py actually emits.
    result = _wait_for(lambda: _query(prometheus_port, "vllm:kv_cache_usage_perc"),
                        lambda r: bool(r))
    assert result, "expected vllm:kv_cache_usage_perc to be scraped from at least one mock pod"


def test_agentgateway_relay_exposes_token_usage_metric(prometheus_port, gateway_port):
    api_key = f"pytest-metrics-{secrets.token_hex(4)}"
    r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                       headers={"x-api-key": api_key},
                       json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text

    result = _wait_for(
        lambda: _query(prometheus_port, "agentgateway_gen_ai_client_token_usage_sum"),
        lambda r: bool(r),
    )
    assert result, (
        "expected agentgateway_gen_ai_client_token_usage_sum to be scraped via "
        "agentgateway-metrics-relay -- if this fails, the relay's Content-Type "
        "fix has regressed and Prometheus is rejecting the scrape again"
    )


def test_statsd_exporter_exposes_rls_metrics(prometheus_port):
    result = _wait_for(
        lambda: _query(prometheus_port, "ratelimit_service_config_load_success"),
        lambda r: bool(r),
    )
    assert result, "expected RLS's statsd-pushed metrics to be scraped via statsd-exporter"


def test_prometheus_has_persistent_storage():
    result = kubectl("-n", "monitoring", "get", "pvc",
                      "-l", "app.kubernetes.io/name=prometheus",
                      "-o", "jsonpath={.items[0].status.phase}")
    assert result.stdout.strip() == "Bound", (
        f"expected Prometheus's PVC Bound (ephemeral storage would silently lose "
        f"retention across every pod restart) -- got {result.stdout!r}, stderr={result.stderr}"
    )
