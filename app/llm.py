import time
from dataclasses import dataclass, field

import httpx


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    api_key: str = ""
    temperature: float | None = 0.2
    timeout: float = 180.0
    # 제공자마다 다른 옵션. 예: Ollama qwen3.5는 reasoning_effort="none"이 없으면 추론을 켬
    extra_body: dict = field(default_factory=dict)


class LLMError(Exception):
    pass


def chat_json(provider: Provider, messages: list[dict]) -> dict:
    headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
    body = {
        "model": provider.model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        **provider.extra_body,
    }
    if provider.temperature is not None:
        body["temperature"] = provider.temperature

    started = time.monotonic()
    try:
        r = httpx.post(f"{provider.base_url}/chat/completions", json=body, headers=headers, timeout=provider.timeout)
        r.raise_for_status()
        data = r.json()
        return {
            "provider": provider.name,
            "content": data["choices"][0]["message"]["content"],
            "prompt_tokens": data["usage"]["prompt_tokens"],
            "completion_tokens": data["usage"]["completion_tokens"],
            "seconds": time.monotonic() - started,
        }
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise LLMError(f"{provider.name}: {e!r}") from e


def chat_json_fallback(providers: list[Provider], messages: list[dict]) -> dict:
    errors = []
    for provider in providers:
        try:
            return chat_json(provider, messages)
        except LLMError as e:
            errors.append(str(e))
    raise LLMError("; ".join(errors))
