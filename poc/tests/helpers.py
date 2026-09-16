"""Shared helpers for the POC test suite: kubectl wrapper + a port-forward
context manager that blocks until the local port actually accepts connections
and cleans up its subprocess on exit. Everything else assumes the environment
is already resumed (podman machine + kind node containers up) -- this suite
verifies components, it doesn't start them.
"""

import os
import signal
import socket
import subprocess
import time

os.environ.setdefault("KUBECONFIG", os.path.expanduser("~/.kube/config"))

KUBECTL_CONTEXT = "kind-inference-poc"

# Matches only *this project's* port-forwards (kubectl --context kind-inference-poc
# ... port-forward), not port-forwards for unrelated projects/contexts the user
# might have running -- deliberately narrower than a blanket `pkill port-forward`.
_STRAY_PATTERN = f"kubectl --context {KUBECTL_CONTEXT}.*port-forward"


def kill_stray_port_forwards():
    """Kill any leftover `kubectl --context kind-inference-poc ... port-forward`
    processes -- e.g. from a manual debugging session that didn't clean up, or a
    previous test run that crashed before its fixtures' __exit__ could fire.
    Safe to call even when nothing matches. Called at both session start (so
    fixtures don't collide with already-bound local ports) and session end (a
    backstop in addition to each PortForward's own __exit__)."""
    result = subprocess.run(["pgrep", "-f", _STRAY_PATTERN], capture_output=True, text=True)
    for pid in result.stdout.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass


def kubectl(*args, timeout=30):
    """Run kubectl against the POC's kind context. Returns the CompletedProcess
    (stdout/stderr as text); doesn't raise on non-zero exit -- callers assert."""
    return subprocess.run(
        ["kubectl", "--context", KUBECTL_CONTEXT, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def pod_name(namespace, label_selector):
    result = kubectl("-n", namespace, "get", "pods", "-l", label_selector,
                      "-o", "jsonpath={.items[0].metadata.name}")
    name = result.stdout.strip()
    if not name:
        raise RuntimeError(
            f"no pod found for -l {label_selector} in {namespace} "
            f"(stderr: {result.stderr.strip()})"
        )
    return name


class PortForward:
    """kubectl port-forward as a context manager. Blocks in __enter__ until the
    local port accepts TCP connections (kubectl's own startup is asynchronous),
    terminates the subprocess in __exit__.

    resource is anything `kubectl port-forward` accepts: "svc/name", "deploy/name".
    """

    def __init__(self, namespace, resource, local_port, remote_port, ready_timeout=15):
        self.namespace = namespace
        self.resource = resource
        self.local_port = local_port
        self.remote_port = remote_port
        self.ready_timeout = ready_timeout
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            ["kubectl", "--context", KUBECTL_CONTEXT, "-n", self.namespace,
             "port-forward", self.resource, f"{self.local_port}:{self.remote_port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"port-forward to {self.resource} in {self.namespace} exited early "
                    f"(exit code {self.proc.returncode}) -- is the pod running?"
                )
            try:
                with socket.create_connection(("localhost", self.local_port), timeout=0.5):
                    return self
            except OSError:
                time.sleep(0.3)
        self.__exit__(None, None, None)
        raise TimeoutError(
            f"port-forward to {self.resource} in {self.namespace} never became "
            f"reachable on localhost:{self.local_port}"
        )

    def __exit__(self, *exc_info):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
