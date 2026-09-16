"""Phase 3 -- Postgres schema + seed data (orgs -> projects -> plans ->
hashed api_keys -> model_allowlist). Read-only: joins the seeded row rather
than mutating anything, so it's safe to rerun freely.
"""

from helpers import kubectl, pod_name

QUERY = """
SELECT o.name, pr.name, pl.name, ak.key_prefix, ma.model_name
FROM orgs o
JOIN projects pr ON pr.org_id = o.id
JOIN plans pl ON pl.id = pr.plan_id
JOIN api_keys ak ON ak.project_id = pr.id
JOIN model_allowlist ma ON ma.project_id = pr.id;
"""


def test_seed_data_joins_correctly():
    pod = pod_name("identity-tenancy", "app=postgres")
    result = kubectl("-n", "identity-tenancy", "exec", pod, "--",
                      "psql", "-U", "identity_tenancy", "-d", "identity_tenancy", "-t", "-c", QUERY)
    assert result.returncode == 0, result.stderr
    assert "acme-poc" in result.stdout
    assert "acme-poc-default" in result.stdout
    assert "free" in result.stdout
    assert "sk-poc-test" in result.stdout
    assert "mock" in result.stdout


def test_api_key_is_not_revoked():
    pod = pod_name("identity-tenancy", "app=postgres")
    result = kubectl("-n", "identity-tenancy", "exec", pod, "--",
                      "psql", "-U", "identity_tenancy", "-d", "identity_tenancy", "-t", "-c",
                      "SELECT revoked_at FROM api_keys WHERE key_prefix = 'sk-poc-test';")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", "seeded key should not be revoked"
