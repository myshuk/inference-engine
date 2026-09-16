"""Phase 3 -- envoyproxy/ratelimit (RLS), tested directly over gRPC via
grpcurl and a minimal hand-written proto (protos/rls-minimal.proto -- same
service/messages/field numbers as the real envoy.service.ratelimit.v3.rls.proto,
without its validate/udpa annotation dependency tree, which doesn't affect wire
compatibility). Requires `grpcurl` on PATH (`brew install grpcurl`).

Each test uses a fresh, randomly-suffixed descriptor value so re-running this
suite never collides with a previous run's counters within the same 1-minute
window -- RLS tracks counters per distinct value it's seen, so a fixed test
key would make later runs see already-depleted budget.
"""

import json
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

PROTO_DIR = Path(__file__).parent / "protos"
DOMAIN = "inference-poc"


@pytest.fixture(scope="module", autouse=True)
def require_grpcurl():
    if shutil.which("grpcurl") is None:
        pytest.skip("grpcurl not on PATH -- brew install grpcurl")


def _should_rate_limit(port, key, value, hits_addend):
    return subprocess.run(
        ["grpcurl", "-plaintext", "-import-path", str(PROTO_DIR), "-proto", "rls-minimal.proto",
         "-d", json.dumps({
             "domain": DOMAIN,
             "descriptors": [{"entries": [{"key": key, "value": value}]}],
             "hits_addend": hits_addend,
         }),
         f"localhost:{port}", "envoy.service.ratelimit.v3.RateLimitService/ShouldRateLimit"],
        capture_output=True, text=True,
    )


def test_rpm_check_and_increment(rls_grpc_port):
    test_key = f"pytest-rpm-{secrets.token_hex(4)}"
    proc = _should_rate_limit(rls_grpc_port, "api_key_rpm", test_key, 1)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["overallCode"] == "OK"
    status = data["statuses"][0]
    assert status["currentLimit"] == {"requestsPerUnit": 60, "unit": "MINUTE"}
    assert status["limitRemaining"] == 59  # fresh descriptor value: 60 - 1


def test_tpm_variable_cost_accumulates(rls_grpc_port):
    test_key = f"pytest-tpm-{secrets.token_hex(4)}"

    first = _should_rate_limit(rls_grpc_port, "api_key_tpm", test_key, 500)
    remaining_after_first = json.loads(first.stdout)["statuses"][0]["limitRemaining"]
    assert remaining_after_first == 9500  # fresh descriptor: 10000 - 500

    second = _should_rate_limit(rls_grpc_port, "api_key_tpm", test_key, 500)
    remaining_after_second = json.loads(second.stdout)["statuses"][0]["limitRemaining"]
    assert remaining_after_second == remaining_after_first - 500, (
        "TPM cost should accumulate across calls against the same descriptor value, "
        "not reset per call"
    )


def test_no_refund_mechanism_exists(rls_grpc_port):
    """D11: 'amend' can only add a durable Kafka billing record (Phase 6, not
    built here) -- it structurally cannot decrement this counter. hits_addend
    is uint32, so a negative value can't even be encoded, and
    RateLimitService exposes exactly one RPC (no Amend/Refund method)."""
    test_key = f"pytest-refund-{secrets.token_hex(4)}"
    proc = _should_rate_limit(rls_grpc_port, "api_key_tpm", test_key, -1)
    assert proc.returncode != 0, (
        "a negative hits_addend should be rejected -- if this starts succeeding, "
        "RLS's API shape has changed and D11's 'no refund path' claim needs re-checking"
    )
