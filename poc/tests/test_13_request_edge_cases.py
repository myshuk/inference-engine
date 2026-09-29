"""Phase 8 -- acceptance criterion 6: the test matrix run against the full
assembled path (agentgateway -> GIE -> mocks -> Band 3), not individual
components in isolation (those already have their own tests in earlier
phases). Covers 4 of the 5 named scenarios; upstream failure lives in
test_14 since it needs a mock_server.py extension to simulate a 5xx/hang.

The two "carry forward, not a named criterion" items from the plan are
already covered elsewhere, not duplicated here: EPP's not-round-robin
behavior is proven by test_03_epp_routing.py (flipping which mock reports
lower load flips every pick -- round robin couldn't produce that), and RLS's
reserved-at-estimate/never-decremented-by-amend property is proven
structurally by test_08_rls.py's test_no_refund_mechanism_exists (no Amend
RPC exists at all, and hits_addend is unsigned so a negative value can't
even be encoded).
"""

import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests


def test_sse_framing_is_well_formed(gateway_port):
    """Not just 'iter_lines() finds a [DONE]' (test_01's level of rigor) --
    parses the raw byte stream directly and checks every event has the
    correct 'data: ' prefix and blank-line terminator."""
    r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                       headers={"x-api-key": f"pytest-sse-{secrets.token_hex(4)}"},
                       json={"model": "mock", "messages": [{"role": "user", "content": "hi"}],
                             "stream": True, "stream_options": {"include_usage": True}},
                       stream=True)
    assert r.status_code == 200
    raw = r.raw.read(decode_content=True)

    events = [e for e in raw.split(b"\n\n") if e]
    assert events, "expected at least one SSE event"

    saw_done = False
    saw_content = False
    for event in events:
        assert event.startswith(b"data: "), f"malformed SSE event, missing 'data: ' prefix: {event!r}"
        payload = event[len(b"data: "):]
        if payload == b"[DONE]":
            saw_done = True
            continue
        chunk = json.loads(payload)  # must be valid JSON -- raises if malformed
        if chunk.get("choices", [{}])[0].get("delta", {}).get("content"):
            saw_content = True

    assert saw_content, "expected at least one content delta chunk"
    assert saw_done, "expected a terminating 'data: [DONE]' event"


def test_disconnect_mid_stream_does_not_hang_the_gateway(gateway_port):
    """Reads one chunk of a streaming response then abruptly closes the
    connection -- proxy for 'the server side drains cleanly': a genuinely
    hung connection/goroutine from the aborted stream would show up as the
    next request stalling, not as an observable server-side crash from a
    black-box pytest client."""
    r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                       headers={"x-api-key": f"pytest-disconnect-{secrets.token_hex(4)}"},
                       json={"model": "mock",
                             "messages": [{"role": "user", "content": "a fixed six word test message"}],
                             "stream": True},
                       stream=True, timeout=10)
    assert r.status_code == 200
    next(r.iter_lines())  # read just the first line...
    r.close()             # ...then abandon the connection mid-stream

    start = time.time()
    r2 = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                        headers={"x-api-key": f"pytest-followup-{secrets.token_hex(4)}"},
                        json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]},
                        timeout=10)
    elapsed = time.time() - start
    assert r2.status_code == 200
    assert elapsed < 5, (
        f"follow-up request took {elapsed:.1f}s after the earlier abrupt disconnect -- "
        "possible hung connection/resource leak"
    )


@pytest.mark.xfail(
    reason=(
        "Known, deliberate gap, not a regression -- API-key identity validation was never "
        "wired up. agentgateway/RLS treat x-api-key as an opaque rate-limit bucket key only; "
        "Postgres's api_keys table (Band 3) is never consulted on the live request path. "
        "agentgateway's native apiKeyAuthentication policy only supports static Secret/ConfigMap "
        "keys (wrong fit -- would make the Secret the source of truth instead of Postgres, per "
        "acceptance criterion 6's own wording). A real fix needs an extAuth-style external check "
        "against Postgres (mirroring how RLS itself is already wired), or a dedicated API gateway "
        "(Kong/Apigee) in front of agentgateway -- evaluated and deferred: Kong doesn't read from "
        "an arbitrary existing Postgres table either, so it would either need its own consumer "
        "store to become the new source of truth (also wrong) or a custom plugin doing the exact "
        "same Postgres check, adding a whole extra gateway hop without removing the work. Treated "
        "as an extended goal, same footing as KEDA (poc/README.md Phase 5) -- ask again later."
    ),
    strict=True,
)
def test_invalid_api_key_is_rejected(gateway_port):
    r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                       headers={"x-api-key": "totally-bogus-key-does-not-exist-in-postgres"},
                       json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code in (401, 403), (
        f"expected an auth rejection for an unrecognized API key, got {r.status_code}: {r.text}"
    )
    assert "choices" not in r.text, "response looks like it reached a mock backend -- should be rejected upstream of it"


def test_rate_limit_rejects_once_bucket_exhausted(gateway_port):
    """RPM limit is 60 (poc/k8s/rls/config.yaml, seeded 'free' plan) -- a
    fresh api_key descriptor should allow exactly 60 requests and reject the
    61st, through the real gateway (not RLS's own gRPC API directly, which
    test_08/test_09 already cover).

    Fired concurrently, not sequentially -- RPM is a real per-minute window,
    and 61 sequential requests through the full stack (agentgateway -> RLS
    gRPC -> mock, each a real network hop) measured at ~64s wall-clock,
    longer than the window itself. Sequentially, the window quietly resets
    partway through and the limit is never actually hit. With concurrency,
    order isn't guaranteed, so this asserts on counts (60 successes, 1
    rejection), not on which specific request number gets rejected.
    """
    api_key = f"pytest-ratelimit-full-{secrets.token_hex(4)}"

    with ThreadPoolExecutor(max_workers=61) as pool:
        futures = [
            pool.submit(requests.post, f"http://localhost:{gateway_port}/v1/chat/completions",
                        headers={"x-api-key": api_key},
                        json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]})
            for _ in range(61)
        ]
        statuses = [f.result().status_code for f in futures]

    ok_count = statuses.count(200)
    rejected_count = statuses.count(429)
    assert ok_count == 60 and rejected_count == 1, (
        f"expected exactly 60 successes and 1 rate-limit rejection out of 61 concurrent "
        f"requests, got {ok_count} successes and {rejected_count} rejections (statuses={statuses})"
    )
