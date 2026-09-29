"""Phase 8 -- acceptance criterion 6, upstream failure scenario: 'a mock/
engine returning 5xx or hanging doesn't take the whole gateway down, and
surfaces as a clean error to the client.' Two genuinely distinct failure
modes, both covered:

- Backend reachable but erroring (poc/k8s/mock-servers/mock_server.py's
  `force_status` control, added for this test) -- lets the failure be
  triggered and reset deterministically without disturbing the mocks'
  normal state for other tests.
- Backend completely unreachable -- scales both mock Deployments to zero
  replicas for real, not simulated at the application level.

EPP picks whichever mock reports the lower load, which doesn't account for
error state or availability at all -- affecting only one mock wouldn't
reliably hit it in either scenario, since EPP could just keep routing to the
other, still-healthy one. Both mocks are always failed/unavailable together.
"""

import secrets
import time

import requests

from helpers import kubectl

NAMESPACE = "inference-poc"


def _mock_pod_phases():
    result = kubectl("-n", NAMESPACE, "get", "pods", "-l", "app=vllm-mock-pool",
                      "-o", "jsonpath={.items[*].status.phase}")
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def _wait_for_pod_phases(predicate, timeout=30, interval=2):
    """A terminated pod briefly shows as Failed/Error before the API server
    fully removes it -- kubectl wait returning doesn't guarantee that last
    step has happened yet. Poll rather than trust a single snapshot."""
    deadline = time.time() + timeout
    phases = _mock_pod_phases()
    while time.time() < deadline:
        phases = _mock_pod_phases()
        if predicate(phases):
            return phases
        time.sleep(interval)
    return phases


def test_upstream_5xx_does_not_take_down_the_gateway(gateway_port, mock_a_port, mock_b_port):
    try:
        requests.post(f"http://localhost:{mock_a_port}/control", json={"force_status": 503})
        requests.post(f"http://localhost:{mock_b_port}/control", json={"force_status": 503})

        start = time.time()
        r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                           headers={"x-api-key": f"pytest-upstream-fail-{secrets.token_hex(4)}"},
                           json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]},
                           timeout=10)
        elapsed = time.time() - start

        assert r.status_code == 503, (
            f"expected the mock's forced upstream failure to surface cleanly to the client, "
            f"got {r.status_code}: {r.text}"
        )
        assert elapsed < 5, f"request took {elapsed:.1f}s -- expected a clean fast failure, not a hang"
    finally:
        # restore the documented baseline (mock-a low, mock-b high, no forced
        # failure) regardless of outcome -- other tests (test_03) assume this
        requests.post(f"http://localhost:{mock_a_port}/control", json={"force_status": 0})
        requests.post(f"http://localhost:{mock_b_port}/control", json={"force_status": 0})

    # The gateway itself should be entirely unaffected by the earlier
    # backend failures -- a follow-up request succeeds immediately.
    r2 = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                        headers={"x-api-key": f"pytest-upstream-followup-{secrets.token_hex(4)}"},
                        json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]},
                        timeout=10)
    assert r2.status_code == 200, "expected the gateway to recover immediately once mocks are healthy again"


def test_upstream_unavailable_does_not_take_down_the_gateway(gateway_port):
    """Stronger than the 5xx case above -- no backend pods exist at all, not
    just an erroring one. Confirmed manually before writing this: with both
    mocks scaled to zero, the gateway returns a clean 503
    ("failed to find endpoint candidates for serving the request") in well
    under a second, not a hang."""
    baseline = _mock_pod_phases()
    assert baseline == ["Running", "Running"], (
        f"expected both mocks Running before disrupting anything, got {baseline} -- "
        "fix the baseline before trusting this test's result"
    )

    try:
        result = kubectl("-n", NAMESPACE, "scale", "deployment", "vllm-mock-a", "vllm-mock-b", "--replicas=0")
        assert result.returncode == 0, result.stderr
        # kubectl's own --timeout and this call's Python-level timeout must
        # not be equal -- if they race, subprocess.TimeoutExpired can fire
        # first and mask kubectl's own (possibly successful) graceful exit.
        kubectl("-n", NAMESPACE, "wait", "--for=delete", "pod", "-l", "app=vllm-mock-pool", "--timeout=30s", timeout=40)

        # --for=delete also exits 0 immediately if the selector matches
        # nothing at invocation time -- doesn't robustly prove the pods are
        # actually gone now. Confirm directly before trusting the state.
        phases = _wait_for_pod_phases(lambda p: p == [])
        assert phases == [], f"expected zero mock pods after scaling to 0, still found: {phases}"

        start = time.time()
        r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                           headers={"x-api-key": f"pytest-upstream-unavailable-{secrets.token_hex(4)}"},
                           json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]},
                           timeout=15)
        elapsed = time.time() - start

        assert r.status_code == 503, (
            f"expected a clean failure with zero backend pods available, got {r.status_code}: {r.text}"
        )
        assert elapsed < 5, f"request took {elapsed:.1f}s -- expected a fast, clean failure, not a hang"
    finally:
        # restore the documented baseline regardless of outcome -- other
        # tests (test_03) assume both mocks exist at 1 replica each
        kubectl("-n", NAMESPACE, "scale", "deployment", "vllm-mock-a", "vllm-mock-b", "--replicas=1")
        kubectl("-n", NAMESPACE, "wait", "--for=condition=Ready", "pod", "-l", "app=vllm-mock-pool", "--timeout=60s", timeout=70)
        restored = _wait_for_pod_phases(lambda p: p == ["Running", "Running"])
        assert restored == ["Running", "Running"], (
            f"expected both mocks Running again after restoring replicas=1, got {restored} -- "
            "cluster may be left in a bad state for later tests"
        )

    r2 = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                        headers={"x-api-key": f"pytest-upstream-unavailable-followup-{secrets.token_hex(4)}"},
                        json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]},
                        timeout=10)
    assert r2.status_code == 200, "expected the gateway to recover immediately once mock pods are back"
