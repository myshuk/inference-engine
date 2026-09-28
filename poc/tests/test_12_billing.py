"""Phase 6 -- Control Plane A billing pipeline (poc/k8s/kafka/,
poc/k8s/clickhouse/, poc/k8s/otel/, poc/k8s/openmeter/). Validates acceptance
criterion 5 for real: a real request produces a correctly-aggregated,
billable usage event end to end, AND a redelivered event doesn't get
double-counted.

agentgateway's own per-request trace ID (only populated because
poc/k8s/otel/access-log-policy.yaml forces tracing with randomSampling:
"1.0" -- without it Trace ID/Span ID are empty) is the event's CloudEvents
`id`. OpenMeter's sink-worker dedupes on (namespace, id, source) via Redis
(poc/k8s/openmeter/values.yaml, pointed at the existing Valkey) before the
ClickHouse insert -- NOT via a ClickHouse ReplacingMergeTree as originally
planned; confirmed by reading OpenMeter's actual source at the exact pinned
tag (v1.0.0-beta.138). The redelivery test produces a raw Kafka message with
a fixed, known id directly (bypassing the HTTP gateway, which can't produce
two real requests sharing the same trace ID) to prove that guarantee for
real, not just assume Redis dedup works because it's configured.
"""

import json
import secrets
import time

import requests

from helpers import kubectl

NAMESPACE = "billing"
CLICKHOUSE_POD = "chi-clickhouse-clickhouse-0-0-0"
CLICKHOUSE_USER = "default"
CLICKHOUSE_PASSWORD = "clickhouse-poc-only"
KAFKA_POD = "kafka-dual-role-0"
KAFKA_TOPIC = "om_default_events"


def _clickhouse_query(sql):
    result = kubectl(
        "-n", NAMESPACE, "exec", CLICKHOUSE_POD, "--",
        "clickhouse-client", "--user", CLICKHOUSE_USER, "--password", CLICKHOUSE_PASSWORD,
        "--database", "openmeter", "--query", sql, "--format", "TabSeparated",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _produce_raw_event(event_id, subject, total_tokens):
    """Writes a message directly onto the same Kafka topic
    poc/k8s/otel/'s transform produces to -- bypasses the HTTP gateway
    entirely so the test can control `id` precisely (needed to simulate a
    redelivery, which a real request can't do since every request gets a
    fresh trace ID). Must match OpenMeter's actual wire schema exactly:
    "time" as a raw Unix-seconds integer and "data" as a JSON-encoded
    string, both confirmed via source, not the plain-CloudEvents shape one
    might guess -- getting either wrong crashes the sink-worker outright on
    this OpenMeter version (see poc/exeReadme.md).
    """
    data = json.dumps({
        "event_version": "1.0",
        "model": "mock",
        "provider": "custom",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": total_tokens,
    })
    envelope = {
        "specversion": "1.0",
        "id": event_id,
        "source": "agentgateway",
        "type": "tokens_used",
        "subject": subject,
        "time": int(time.time()),
        "data": data,
    }
    # namespace header is required -- OpenMeter's sink-worker reads the
    # tenant namespace from a Kafka message HEADER, not the JSON body.
    line = f"namespace:default\t{json.dumps(envelope)}\n"
    result = kubectl(
        "-n", NAMESPACE, "exec", "-i", KAFKA_POD, "-c", "kafka", "--",
        "bin/kafka-console-producer.sh", "--bootstrap-server", "localhost:9092",
        "--topic", KAFKA_TOPIC, "--property", "parse.headers=true",
        input_text=line,
    )
    assert result.returncode == 0, result.stderr


def _wait_for(fn, predicate, timeout=30, interval=2):
    """The full pipeline (Collector transform -> Kafka -> sink-worker ->
    ClickHouse insert -> meter aggregation) is asynchronous end to end --
    poll rather than assume it's already settled right after producing."""
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        result = fn()
        if predicate(result):
            return result
        time.sleep(interval)
    return result


def _query_meter(openmeter_port, subject):
    resp = requests.get(f"http://localhost:{openmeter_port}/api/v1/meters/tokens_total/query",
                         params={"subject": subject}, timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]


def test_real_request_produces_a_correctly_aggregated_usage_event(gateway_port, openmeter_port):
    api_key = f"pytest-billing-{secrets.token_hex(4)}"
    r = requests.post(f"http://localhost:{gateway_port}/v1/chat/completions",
                       headers={"x-api-key": api_key},
                       json={"model": "mock",
                             "messages": [{"role": "user", "content": "a fixed six word test message"}],
                             "max_tokens": 42})
    assert r.status_code == 200, r.text
    expected_tokens = r.json()["usage"]["total_tokens"]

    result = _wait_for(lambda: _query_meter(openmeter_port, api_key), lambda d: bool(d))
    assert result, f"expected a meter row for subject={api_key}"
    assert result[0]["value"] == expected_tokens, (
        f"expected aggregated usage to equal the real response's total_tokens "
        f"({expected_tokens}), got {result[0]['value']}"
    )


def test_redelivered_event_id_is_not_double_counted(openmeter_port):
    event_id = f"pytest-dedup-{secrets.token_hex(6)}"
    subject = f"pytest-dedup-subject-{secrets.token_hex(4)}"

    _produce_raw_event(event_id, subject, total_tokens=150)
    _produce_raw_event(event_id, subject, total_tokens=150)  # exact redelivery, same id

    count = _wait_for(
        lambda: int(_clickhouse_query(f"SELECT count(*) FROM om_events WHERE id='{event_id}'")),
        lambda c: c > 0,
    )
    assert count == 1, f"expected exactly 1 row for a redelivered event id, got {count}"

    result = _wait_for(lambda: _query_meter(openmeter_port, subject), lambda d: bool(d))
    assert result and result[0]["value"] == 150, (
        f"expected the duplicate delivery to be a no-op (value=150, not 300) -- "
        f"OpenMeter's Redis dedupe should have caught the redelivered id, got {result}"
    )
