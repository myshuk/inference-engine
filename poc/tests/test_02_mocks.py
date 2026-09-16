"""Phase 2 -- the two mock backends behind vllm-mock-pool, tested directly
(port-forwarded straight to each pod, bypassing the gateway/EPP entirely).
Restores each mock's /control state to what it found, so this test doesn't
leave load values altered for other tests or manual poking afterward.
"""

import pytest
import requests


@pytest.fixture(params=["mock_a_port", "mock_b_port"])
def mock_port(request):
    return request.getfixturevalue(request.param)


def test_health(mock_port):
    r = requests.get(f"http://localhost:{mock_port}/health")
    assert r.status_code == 200


def test_metrics_exposes_real_vllm_metric_names(mock_port):
    r = requests.get(f"http://localhost:{mock_port}/metrics")
    assert "vllm:num_requests_waiting" in r.text
    assert "vllm:kv_cache_usage_perc" in r.text


def test_chat_completions_non_streaming(mock_port):
    r = requests.post(f"http://localhost:{mock_port}/v1/chat/completions", json={
        "model": "mock", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    usage = r.json()["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_chat_completions_streaming_with_usage(mock_port):
    r = requests.post(f"http://localhost:{mock_port}/v1/chat/completions", json={
        "model": "mock", "messages": [{"role": "user", "content": "hi"}],
        "stream": True, "stream_options": {"include_usage": True},
    })
    assert r.status_code == 200
    assert "data: [DONE]" in r.text
    assert '"usage"' in r.text


def test_control_endpoint_round_trips(mock_port):
    original = requests.get(f"http://localhost:{mock_port}/control").json()
    try:
        r = requests.post(f"http://localhost:{mock_port}/control",
                           json={"num_requests_waiting": 12345})
        assert r.json()["num_requests_waiting"] == 12345.0
    finally:
        requests.post(f"http://localhost:{mock_port}/control", json=original)
