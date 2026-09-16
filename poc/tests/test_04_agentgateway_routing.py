"""Phase 2 / acceptance criterion 4 -- the naive HTTPRoute -> InferencePool
backendRef doesn't feed agentgateway's own token-usage metering, while
HTTPRoute -> AgentgatewayBackend -> custom provider -> InferencePool does.
Both configs route successfully (inferenceExtension.enabled fixed that for
the naive path too) -- the difference under test is metering, not routing.

This test mutates the live HTTPRoute object via kubectl patch. The
restore_corrected_route fixture guarantees the corrected config is reapplied
even if a test fails partway through, since every other test in this suite
(and manual use of the cluster) assumes that resting state.
"""

import json
import time

import pytest
import requests

from helpers import kubectl

NAIVE_BACKEND = [{
    "group": "inference.networking.k8s.io", "kind": "InferencePool",
    "name": "vllm-mock-pool", "port": 8000,
}]
CORRECTED_BACKEND = [{
    "group": "agentgateway.dev", "kind": "AgentgatewayBackend",
    "name": "vllm-mock-pool-backend", "weight": 1,
}]


def _set_backend(refs):
    patch = json.dumps([{"op": "replace", "path": "/spec/rules/0/backendRefs", "value": refs}])
    result = kubectl("-n", "inference-poc", "patch", "httproute",
                      "vllm-mock-pool-inference-route", "--type=json", "-p", patch)
    assert result.returncode == 0, result.stderr
    time.sleep(2)  # let agentgateway's controller reconcile the new xDS config


def _token_usage(stats_port):
    """Returns {"count": {"input": n, "output": n}, "sum": {"input": n, "output": n}}.
    _count is the number of observations (increments by 1 per request, regardless
    of size) -- _sum is the actual running total of token counts. Comparing
    completion_tokens against _count instead of _sum was the first version of
    this test's own bug: _count only ever moves by 1, so it can't detect whether
    the *right number* of tokens got metered, only that *a* request was metered.
    """
    r = requests.get(f"http://localhost:{stats_port}/metrics")
    result = {"count": {}, "sum": {}}
    for line in r.text.splitlines():
        for metric, key in (("agentgateway_gen_ai_client_token_usage_count", "count"),
                             ("agentgateway_gen_ai_client_token_usage_sum", "sum")):
            if line.startswith(metric):
                token_type = "output" if 'gen_ai_token_type="output"' in line else "input"
                result[key][token_type] = float(line.rsplit(" ", 1)[1])
    return result


@pytest.fixture
def restore_corrected_route():
    yield
    _set_backend(CORRECTED_BACKEND)


def test_naive_inferencepool_route_does_not_meter_tokens(
    gateway_port, agentgateway_stats_port, restore_corrected_route
):
    _set_backend(NAIVE_BACKEND)
    before = _token_usage(agentgateway_stats_port)

    r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions", json={
        "model": "mock", "messages": [{"role": "user", "content": "naive path test"}],
    })
    assert r.status_code == 200
    assert "usage" in r.json(), "client should still see usage -- it's from the mock's own JSON body"

    after = _token_usage(agentgateway_stats_port)
    assert after == before, (
        "agentgateway's own token metric should NOT change via the naive route "
        f"(before={before}, after={after})"
    )


def test_corrected_route_meters_tokens(gateway_port, agentgateway_stats_port):
    _set_backend(CORRECTED_BACKEND)
    before = _token_usage(agentgateway_stats_port)

    r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions", json={
        "model": "mock", "messages": [{"role": "user", "content": "corrected path test"}],
    })
    assert r.status_code == 200
    usage = r.json()["usage"]

    after = _token_usage(agentgateway_stats_port)
    # _count: exactly one more observation of each type
    assert after["count"].get("output", 0) == before["count"].get("output", 0) + 1
    assert after["count"].get("input", 0) == before["count"].get("input", 0) + 1
    # _sum: increased by exactly this response's real token counts, not just "some amount"
    assert after["sum"].get("output", 0) == before["sum"].get("output", 0) + usage["completion_tokens"]
    assert after["sum"].get("input", 0) == before["sum"].get("input", 0) + usage["prompt_tokens"]
