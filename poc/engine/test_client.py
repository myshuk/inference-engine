"""Acceptance criteria 1 & 2: official OpenAI SDK against the native vLLM engine,
both streaming and non-streaming, with correct usage."""

from openai import OpenAI

client = OpenAI(base_url="http://localhost:8001/v1", api_key="not-needed")
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"


def test_non_streaming():
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "What is 2+2? Answer in one word."}],
        max_tokens=20,
    )
    print("--- non-streaming ---")
    print("content:", resp.choices[0].message.content)
    print("usage:", resp.usage)
    assert resp.usage is not None
    assert resp.usage.total_tokens == resp.usage.prompt_tokens + resp.usage.completion_tokens


def test_streaming():
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Count from 1 to 5."}],
        max_tokens=40,
        stream=True,
        stream_options={"include_usage": True},
    )
    print("--- streaming ---")
    chunks = []
    usage = None
    for event in stream:
        if event.choices and event.choices[0].delta.content:
            chunks.append(event.choices[0].delta.content)
        if event.usage:
            usage = event.usage
    print("content:", "".join(chunks))
    print("usage:", usage)
    assert usage is not None


if __name__ == "__main__":
    test_non_streaming()
    test_streaming()
    print("\nAll checks passed.")
