"""Phase 1 -- acceptance criteria 1 & 2: real vLLM (vllm-metal), native host
process, no gateway in front. Not managed by this suite's port-forward fixtures
since it isn't in-cluster -- skips cleanly if it isn't already running
(`vllm serve Qwen/Qwen2.5-1.5B-Instruct --port 8001`), rather than failing.
"""

import json

import pytest
import requests

VLLM_URL = "http://localhost:8001"
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"


@pytest.fixture(scope="module", autouse=True)
def require_vllm_running():
    try:
        requests.get(f"{VLLM_URL}/health", timeout=2)
    except requests.exceptions.ConnectionError:
        pytest.skip(
            "native vLLM not running on :8001 -- start with "
            "`vllm serve Qwen/Qwen2.5-1.5B-Instruct --port 8001` to include this phase"
        )


def test_non_streaming_chat_completion():
    r = requests.post(f"{VLLM_URL}/v1/chat/completions", json={
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say hi in one word."}],
        "max_tokens": 10,
    })
    assert r.status_code == 200
    usage = r.json()["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_streaming_sse_framing():
    r = requests.post(f"{VLLM_URL}/v1/chat/completions", json={
        "model": MODEL,
        "messages": [{"role": "user", "content": "Count from 1 to 3."}],
        "max_tokens": 20,
        "stream": True,
    }, stream=True)
    assert r.status_code == 200
    saw_done = False
    saw_content = False
    for line in r.iter_lines():
        if not line:
            continue
        if line == b"data: [DONE]":
            saw_done = True
            continue
        chunk = json.loads(line[len(b"data: "):])
        if chunk["choices"][0]["delta"].get("content"):
            saw_content = True
    assert saw_content and saw_done


def test_streaming_usage_requires_explicit_opt_in():
    r = requests.post(f"{VLLM_URL}/v1/chat/completions", json={
        "model": MODEL,
        "messages": [{"role": "user", "content": "Count from 1 to 3."}],
        "max_tokens": 20,
        "stream": True,
        "stream_options": {"include_usage": True},
    }, stream=True)
    saw_usage = False
    for line in r.iter_lines():
        if not line or line == b"data: [DONE]":
            continue
        chunk = json.loads(line[len(b"data: "):])
        if chunk.get("usage"):
            saw_usage = True
    assert saw_usage, "expected a final usage-only chunk before [DONE] with include_usage=True"


def test_official_openai_sdk():
    openai = pytest.importorskip("openai")
    client = openai.OpenAI(base_url=f"{VLLM_URL}/v1", api_key="not-needed")
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "What is 2+2? Answer in one word."}],
        max_tokens=10,
    )
    assert resp.usage.total_tokens == resp.usage.prompt_tokens + resp.usage.completion_tokens
