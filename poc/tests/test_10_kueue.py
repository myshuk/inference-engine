"""Phase 4 -- Kueue installed for real (poc/k8s/kueue-lab/), scheduling
against a fabricated nvidia.com/gpu extended resource patched onto the kind
worker node's status (no real GPU hardware exists on this Mac -- see
exeReadme.md for the `kubectl patch --subresource=status` command). Assumes
the ResourceFlavor/ClusterQueue/Namespace/LocalQueue in
poc/k8s/kueue-lab/fake-gpu-queue.yaml are already applied (static infra, like
Postgres's schema -- not something this test deploys itself).

Validates actual quota enforcement, not just that the queue objects report
Ready: submits more GPU-requesting jobs than the ClusterQueue's quota allows
and confirms Kueue admits exactly up to quota and holds the rest suspended.
Uses a unique job-name suffix per test run so leftover jobs from an
interrupted previous run can't eat into this run's quota.
"""

import secrets
import time

from helpers import kubectl

NAMESPACE = "gpu-jobs"
QUEUE_NAME = "gpu-queue"
CLUSTER_QUEUE_GPU_QUOTA = 2
JOB_COUNT = 3  # deliberately one more than quota, to force exactly one to queue


def _job_manifest(name):
    return f"""
apiVersion: batch/v1
kind: Job
metadata:
  name: {name}
  namespace: {NAMESPACE}
  labels:
    kueue.x-k8s.io/queue-name: {QUEUE_NAME}
spec:
  suspend: true
  template:
    spec:
      containers:
        - name: worker
          image: busybox:1.36
          command: ["sh", "-c", "sleep 60"]
          resources:
            requests: {{cpu: "50m", memory: "32Mi", nvidia.com/gpu: "1"}}
            limits: {{cpu: "50m", memory: "32Mi", nvidia.com/gpu: "1"}}
      restartPolicy: Never
"""


def _is_suspended(job_name):
    result = kubectl("-n", NAMESPACE, "get", "job", job_name, "-o", "jsonpath={.spec.suspend}")
    return result.stdout.strip() == "true"


def test_quota_admits_exactly_up_to_limit_and_queues_the_rest():
    suffix = secrets.token_hex(4)
    names = [f"gpu-job-{suffix}-{i}" for i in range(JOB_COUNT)]

    try:
        manifest = "\n---\n".join(_job_manifest(n) for n in names)
        result = kubectl("apply", "-f", "-", input_text=manifest)
        assert result.returncode == 0, result.stderr

        # Kueue's admission is async (its own controller flips spec.suspend
        # after evaluating quota) -- poll briefly rather than assume it has
        # already settled immediately after job creation.
        expected_suspended = JOB_COUNT - CLUSTER_QUEUE_GPU_QUOTA
        states = {}
        deadline = time.time() + 15
        while time.time() < deadline:
            states = {n: _is_suspended(n) for n in names}
            if sum(states.values()) == expected_suspended:
                break
            time.sleep(1)

        assert sum(states.values()) == expected_suspended, (
            f"expected exactly {expected_suspended} of {JOB_COUNT} jobs "
            f"(quota={CLUSTER_QUEUE_GPU_QUOTA}) to remain suspended, got states={states}"
        )
    finally:
        kubectl("-n", NAMESPACE, "delete", "job", *names, "--ignore-not-found", "--wait=false")
