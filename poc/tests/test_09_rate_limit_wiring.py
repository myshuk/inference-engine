"""Phase 3 step 5 -- agentgateway's traffic-policy stage wired to call RLS for
real (poc/k8s/rls/agentgateway-policy.yaml + referencegrant.yaml). Validates
that the REAL per-request token count reaches RLS's counter, not a flat/fixed
cost -- the exact discrepancy debugged at length in exeReadme.md (traced to
stale internal state on a long-running data-plane pod, not a code or config
bug -- fixed by a pod restart).

Uses a fresh, randomly-suffixed x-api-key value per test so the RLS
descriptor starts from a clean, never-seen counter -- no interference from
other tests' or manual runs' accumulated state on the shared seeded key.
"""

import json
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import requests

PROTO_DIR = Path(__file__).parent / "protos"
DOMAIN = "inference-poc"


@pytest.fixture(scope="module", autouse=True)
def require_grpcurl():
    if shutil.which("grpcurl") is None:
        pytest.skip("grpcurl not on PATH -- brew install grpcurl")


def _remaining(rls_port, key, value):
    """Zero-cost 'peek' isn't actually zero-cost (see test_08) -- a
    hits_addend=0 call still adds 1, per RLS's documented "not set" fallback.
    Callers must account for that +1 themselves."""
    proc = subprocess.run(
        ["grpcurl", "-plaintext", "-import-path", str(PROTO_DIR), "-proto", "rls-minimal.proto",
         "-d", json.dumps({
             "domain": DOMAIN,
             "descriptors": [{"entries": [{"key": key, "value": value}]}],
             "hits_addend": 0,
         }),
         f"localhost:{rls_port}", "envoy.service.ratelimit.v3.RateLimitService/ShouldRateLimit"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["statuses"][0]["limitRemaining"]


def _wait_for_remaining(rls_port, key, value, predicate, timeout=5, interval=0.5):
    """agentgateway's TPM 'amend' call is fire-and-forget async (spawned, not
    awaited before the HTTP response returns to the client) -- poll briefly
    rather than assume it's already landed by the time our own request returns."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _remaining(rls_port, key, value)
        if predicate(last):
            return last
        time.sleep(interval)
    return last


def test_rpm_descriptor_gets_a_real_hit(gateway_port, rls_grpc_port):
    api_key = f"pytest-wiring-rpm-{secrets.token_hex(4)}"

    r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                       headers={"x-api-key": api_key},
                       json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text

    # RPM is checked synchronously pre-dispatch (unlike TPM's async amend), so
    # no polling needed: fresh descriptor (60) - this request's cost (1) -
    # this peek's own +1 quirk = 58.
    remaining = _remaining(rls_grpc_port, "api_key_rpm", api_key)
    assert remaining == 58


def test_agentgateway_charges_real_token_count_to_rls(gateway_port, rls_grpc_port):
    api_key = f"pytest-wiring-tpm-{secrets.token_hex(4)}"

    r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                       headers={"x-api-key": api_key},
                       json={"model": "mock",
                             "messages": [{"role": "user", "content": "a fixed six word test message"}],
                             "max_tokens": 42})
    assert r.status_code == 200, r.text
    usage = r.json()["usage"]

    # fresh descriptor (10000) - the real amend cost (usage.total_tokens) -
    # this peek's own +1 quirk. The "estimate" half of the call contributes 0
    # (no tokenizer configured, confirmed in exeReadme.md's trace-log dig).
    expected = 10000 - usage["total_tokens"] - 1
    remaining = _wait_for_remaining(rls_grpc_port, "api_key_tpm", api_key,
                                     predicate=lambda r: r == expected)
    assert remaining == expected, (
        f"expected RLS to have been charged the real total_tokens ({usage['total_tokens']}), "
        f"not a flat/fixed cost -- remaining={remaining}, expected={expected}"
    )
