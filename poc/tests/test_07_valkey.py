"""Phase 3 -- Valkey, the cache RLS's counters live in. Uses a test-specific
key (not anything RLS or Postgres cares about) so this never interferes with
the counters test_08_rls.py exercises.
"""

from helpers import kubectl, pod_name


def test_set_and_get_round_trip():
    pod = pod_name("identity-tenancy", "app=valkey")
    set_result = kubectl("-n", "identity-tenancy", "exec", pod, "--",
                          "valkey-cli", "set", "pytest-roundtrip-key", "pytest-value")
    assert set_result.returncode == 0 and "OK" in set_result.stdout

    get_result = kubectl("-n", "identity-tenancy", "exec", pod, "--",
                          "valkey-cli", "get", "pytest-roundtrip-key")
    assert get_result.stdout.strip() == "pytest-value"

    kubectl("-n", "identity-tenancy", "exec", pod, "--", "valkey-cli", "del", "pytest-roundtrip-key")
