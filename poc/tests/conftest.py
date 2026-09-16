"""Session-scoped port-forward fixtures shared across test files. Each one
sets up its kubectl port-forward once per test run (not per test -- avoids
repeated subprocess churn) and tears it down when the whole session ends.

Port numbers match what's used throughout poc/README.md and exeReadme.md for
manual verification, so a human following those docs and this suite land on
the same local ports.
"""

import pytest

from helpers import PortForward, kill_stray_port_forwards


def pytest_sessionstart(session):
    """Pre-test cleanup: kill any leftover port-forwards from a previous crashed
    run or manual debugging session before this run's fixtures try to bind the
    same local ports."""
    kill_stray_port_forwards()


def pytest_sessionfinish(session, exitstatus):
    """Post-test cleanup: each PortForward fixture already tears down its own
    subprocess via __exit__, but this is a backstop in case pytest itself was
    interrupted (e.g. Ctrl-C) before fixture teardown could run."""
    kill_stray_port_forwards()


@pytest.fixture(scope="session")
def gateway_port():
    with PortForward("inference-poc", "svc/inference-gateway", 18080, 80) as pf:
        yield pf.local_port


@pytest.fixture(scope="session")
def mock_a_port():
    with PortForward("inference-poc", "deploy/vllm-mock-a", 18000, 8000) as pf:
        yield pf.local_port


@pytest.fixture(scope="session")
def mock_b_port():
    with PortForward("inference-poc", "deploy/vllm-mock-b", 18001, 8000) as pf:
        yield pf.local_port


@pytest.fixture(scope="session")
def agentgateway_admin_port():
    with PortForward("inference-poc", "deploy/inference-gateway", 15000, 15000) as pf:
        yield pf.local_port


@pytest.fixture(scope="session")
def agentgateway_stats_port():
    with PortForward("inference-poc", "deploy/inference-gateway", 15020, 15020) as pf:
        yield pf.local_port


@pytest.fixture(scope="session")
def keycloak_port():
    with PortForward("keycloak", "svc/keycloak-keycloakx-http", 8180, 80) as pf:
        yield pf.local_port


@pytest.fixture(scope="session")
def rls_grpc_port():
    with PortForward("identity-tenancy", "svc/rls", 8081, 8081) as pf:
        yield pf.local_port
