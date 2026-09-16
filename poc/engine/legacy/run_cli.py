from engine import BasicInferenceEngine, InferenceRequest


def main():
    engine = BasicInferenceEngine(model_name="distilgpt2")

    print("\nBasic Inference Engine CLI")
    print("Type 'exit' to quit.\n")

    while True:
        prompt = input("Prompt > ")

        if prompt.lower().strip() in ["exit", "quit"]:
            print("Exiting.")
            break

        request = InferenceRequest(
            prompt=prompt,
            max_new_tokens=80,
            temperature=0.7,
            top_p=0.9
        )

        response = engine.generate(request)

        print("\n--- Response ---")
        print(response.output_text)

        print("\n--- Metrics ---")
        print(f"Model: {response.model_name}")
        print(f"Device: {response.device}")
        print(f"Latency: {response.latency_ms} ms")
        print(f"Input tokens: {response.input_tokens}")
        print(f"Output tokens: {response.output_tokens}")
        print("----------------\n")


if __name__ == "__main__":
    main()