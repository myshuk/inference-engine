"""Phase 2 / acceptance criterion 3 -- EPP's queue-scorer + kv-cache-utilization-scorer
route to whichever mock reports lower load, through the real gateway path (not
bypassed). See poc/epp_scoring.md for the exact scoring math this proves out.

Assumes the HTTPRoute is currently wired through AgentgatewayBackend (the
resting state this repo leaves things in) -- test_04 exercises the naive
backendRef separately and restores this one afterward regardless of outcome.
"""

import time

import requests

LOW = {"num_requests_waiting": 0, "kv_cache_usage_perc": 0.05}
HIGH = {"num_requests_waiting": 50, "kv_cache_usage_perc": 0.9}

# EPP scrapes each pod's own /metrics on a short interval and separately
# re-exports its own Prometheus stats every 5s (refresh-prometheus-metrics-interval)
# -- found by testing without a wait at all: the first request after a /control
# flip could still see the pre-flip pick. 10s gives comfortable margin over both
# without making a suite meant for frequent re-runs sluggish.
METRICS_SETTLE_SECONDS = 10


def _ask(gateway_port, n=5):
    picks = []
    for i in range(n):
        r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions", json={
            "model": "mock", "messages": [{"role": "user", "content": f"req {i}"}],
        })
        assert r.status_code == 200, r.text
        picks.append(r.json()["choices"][0]["message"]["content"])
    return picks


def test_routes_to_low_load_pod(gateway_port, mock_a_port, mock_b_port):
    requests.post(f"http://localhost:{mock_a_port}/control", json=LOW)
    requests.post(f"http://localhost:{mock_b_port}/control", json=HIGH)
    time.sleep(METRICS_SETTLE_SECONDS)

    picks = _ask(gateway_port)
    assert all("vllm-mock-a" in p for p in picks), picks


def test_selection_flips_when_load_flips(gateway_port, mock_a_port, mock_b_port):
    try:
        requests.post(f"http://localhost:{mock_a_port}/control", json=HIGH)
        requests.post(f"http://localhost:{mock_b_port}/control", json=LOW)
        time.sleep(METRICS_SETTLE_SECONDS)

        picks = _ask(gateway_port)
        assert all("vllm-mock-b" in p for p in picks), picks
    finally:
        # restore the documented baseline (mock-a low, mock-b high) for other
        # tests and for anyone poking at the cluster manually afterward
        requests.post(f"http://localhost:{mock_a_port}/control", json=LOW)
        requests.post(f"http://localhost:{mock_b_port}/control", json=HIGH)
        time.sleep(METRICS_SETTLE_SECONDS)
