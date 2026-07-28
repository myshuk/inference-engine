# inference-engine

Inference-as-a-service on GPUs we own in our own datacenter. Customers call an OpenAI-compatible
API, we serve tokens, we bill per token. vLLM runs as the execution core; product differentiation
lives in the platform layer around it.

This repo currently holds the architecture design — base code for the engine itself lands here as
it's built.

- **[Architecture wiki](wiki/Home.md)** — full write-up: the six-band request path, control planes
  (billing, identity, model registry, observability), MVP scope, and open decisions.
- **[inference-stack-v3.drawio](inference-stack-v3.drawio)** — the stack diagram behind the wiki.
  Open with [diagrams.net](https://app.diagrams.net) or the [VS Code Draw.io
  extension](https://marketplace.visualstudio.com/items?itemName=hediet.vscode-drawio).
