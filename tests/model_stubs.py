"""External boundaries of model setup in tests (`mnema model`, the Mac model window): the
provider's model list (providers.TRANSPORT) and litellm.completion."""

from __future__ import annotations

import types

import httpx

from mnema.llm import providers

GEMINI_MODELS = ("gemini-pro-latest", "gemini-flash-latest", "gemini-flash-lite-latest")


def gemini(monkeypatch, *good: str) -> list[str]:
    """Gemini: the model list by GET only, the key in a header only; good keys get 200 and
    GEMINI_MODELS, others 400 "API key not valid". Records the keys of incoming requests."""
    keys: list[str] = []
    listing = {
        "models": [
            {"name": f"models/{name}", "supportedGenerationMethods": ["generateContent"]}
            for name in GEMINI_MODELS
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and "key=" not in str(request.url)
        key = request.headers["x-goog-api-key"]
        keys.append(key)
        if key in good:
            return httpx.Response(200, json=listing)
        return httpx.Response(400, json={"error": {"message": "API key not valid."}})

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(handler))
    return keys


def error(text: str, code: int | None) -> Exception:
    """An exception shaped like litellm's: text and status_code."""
    e = Exception(text)
    e.status_code = code  # type: ignore[attr-defined]
    return e


def chunk(text: str):
    """A chunk of a litellm streaming reply."""
    delta = types.SimpleNamespace(content=text)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta, finish_reason="stop")])


class Litellm:
    """litellm.completion: answers[model] is a text, an exception, or a list of them per call
    ("готово" by default); calls holds call arguments; sleeps the retry pauses (not waited)."""

    def __init__(self, monkeypatch) -> None:
        import litellm

        from mnema.llm import claude

        self.calls: list[dict] = []
        self.answers: dict[str, object] = {}
        self.sleeps: list[float] = []
        monkeypatch.setattr(litellm, "completion", self._completion)
        monkeypatch.setattr(claude, "time", types.SimpleNamespace(sleep=self.sleeps.append))

    def _completion(self, **kw):
        self.calls.append(kw)
        out = self.answers.get(kw["model"], "готово")
        if isinstance(out, list):
            out = out.pop(0)
        if isinstance(out, BaseException):
            raise out
        return iter([chunk(str(out))])
