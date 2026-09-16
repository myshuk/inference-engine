import time
import torch
from dataclasses import dataclass
from transformers import AutoTokenizer, AutoModelForCausalLM


@dataclass
class InferenceRequest:
    prompt: str
    max_new_tokens: int = 80
    temperature: float = 0.7
    top_p: float = 0.9


@dataclass
class InferenceResponse:
    output_text: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    device: str
    model_name: str


class BasicInferenceEngine:
    """
    A minimal local inference engine.

    Responsibilities:
    1. Select device
    2. Load tokenizer
    3. Load model
    4. Accept prompt
    5. Tokenise input
    6. Run model.generate()
    7. Decode output tokens
    8. Return response with basic metrics
    """

    def __init__(self, model_name: str = "distilgpt2"):
        self.model_name = model_name
        self.device = self._select_device()

        print(f"Loading tokenizer for {self.model_name}...")
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)

        print(f"Loading model {self.model_name} on {self.device}...")
        self.model = AutoModelForCausalLM.from_pretrained(self.model_name)
        self.model.to(self.device)
        self.model.eval()

        # GPT-2 style models may not have a pad token by default.
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        print("Inference engine ready.")

    def _select_device(self) -> str:
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def generate(self, request: InferenceRequest) -> InferenceResponse:
        start_time = time.time()

        inputs = self.tokenizer(
            request.prompt,
            return_tensors="pt",
            padding=True,
            truncation=True
        )

        input_ids = inputs["input_ids"].to(self.device)
        attention_mask = inputs["attention_mask"].to(self.device)

        input_token_count = input_ids.shape[-1]

        with torch.no_grad():
            outputs = self.model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                max_new_tokens=request.max_new_tokens,
                do_sample=True,
                temperature=request.temperature,
                top_p=request.top_p,
                pad_token_id=self.tokenizer.eos_token_id
            )

        output_token_count = outputs.shape[-1] - input_token_count

        decoded_text = self.tokenizer.decode(
            outputs[0],
            skip_special_tokens=True
        )

        latency_ms = (time.time() - start_time) * 1000

        return InferenceResponse(
            output_text=decoded_text,
            latency_ms=round(latency_ms, 2),
            input_tokens=input_token_count,
            output_tokens=output_token_count,
            device=self.device,
            model_name=self.model_name
        )