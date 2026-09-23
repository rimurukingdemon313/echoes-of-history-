"""Google Gemini via the Generative Language REST API."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ...errors import ConfigError, Permanent, ProviderUnavailable, RateLimited
from ...logging import get_logger
from .. import http as http_util
from .base import parse_json_strict

log = get_logger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# The model declining to answer is not a transport problem and must not be
# retried into a quota hole. Each of these ends the call.
_TERMINAL_FINISH = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}


class GeminiProvider:
    name = "gemini"

    def __init__(
        self, api_key: str | None, model: str, heavy_model: str,
        *, max_output_tokens: int = 8192, temperature: float = 0.7,
        timeout_s: float = 180.0,
    ) -> None:
        self._key = api_key
        self._model = model
        self._heavy = heavy_model
        self._max_tokens = max_output_tokens
        self._temperature = temperature
        self._timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=60.0, pool=10.0)

    def available(self) -> bool:
        return bool(self._key)

    def _call(
        self, prompt: str, *, system: str | None, heavy: bool,
        max_tokens: int | None, temperature: float | None, json_mode: bool,
    ) -> str:
        if not self._key:
            raise ConfigError(
                "GEMINI_API_KEY is not set. The script, fact-check and metadata "
                "stages cannot run without it; set it, or run with DRY_RUN=true "
                "and LLM_PROVIDER=offline."
            )
        model = self._heavy if heavy else self._model
        config: dict[str, Any] = {
            "temperature": self._temperature if temperature is None else temperature,
            "maxOutputTokens": max_tokens or self._max_tokens,
        }
        if json_mode:
            config["responseMimeType"] = "application/json"

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": config,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        payload = self._request(model, body)
        return self._extract_text(payload, model)

    def _request(self, model: str, body: dict[str, Any]) -> Any:
        url = _ENDPOINT.format(model=model)
        headers = {"x-goog-api-key": self._key or "", "content-type": "application/json"}
        # A generation call is not idempotent in cost, but it has no external
        # side effect, so a transport failure is safe to surface as retryable
        # and let the stage runner decide.
        with httpx.Client(timeout=self._timeout) as client:
            try:
                response = client.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise ProviderUnavailable(f"gemini request failed: {exc}") from exc
        if response.status_code == 429:
            raise RateLimited("gemini rate limited", http_util._retry_after(response))
        if response.status_code >= 500:
            raise ProviderUnavailable(f"gemini returned {response.status_code}")
        if response.status_code >= 400:
            # The body can echo the key in an error about the key; never log it raw.
            raise Permanent(
                f"gemini returned {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    @staticmethod
    def _extract_text(payload: Any, model: str) -> str:
        candidates = (payload or {}).get("candidates") or []
        if not candidates:
            feedback = (payload or {}).get("promptFeedback", {})
            raise Permanent(
                f"gemini returned no candidates (model={model}, "
                f"blockReason={feedback.get('blockReason', 'none')})"
            )
        candidate = candidates[0]
        finish = candidate.get("finishReason")
        if finish in _TERMINAL_FINISH:
            raise Permanent(f"gemini refused to answer: finishReason={finish}")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise Permanent(f"gemini returned an empty response (finishReason={finish})")
        if finish == "MAX_TOKENS":
            # Surfaced rather than swallowed: a truncated chapter must not be
            # quietly narrated as though it were complete.
            raise Permanent(
                "gemini response hit MAX_TOKENS and is truncated; reduce the "
                "requested chunk size rather than accepting a cut-off chapter"
            )
        return text

    def generate(
        self, prompt: str, *, system: str | None = None, heavy: bool = False,
        max_tokens: int | None = None, temperature: float | None = None,
    ) -> str:
        return self._call(prompt, system=system, heavy=heavy, max_tokens=max_tokens,
                          temperature=temperature, json_mode=False)

    def generate_json(
        self, prompt: str, *, system: str | None = None, heavy: bool = False,
        max_tokens: int | None = None,
    ) -> Any:
        raw = self._call(prompt, system=system, heavy=heavy, max_tokens=max_tokens,
                         temperature=0.2, json_mode=True)
        try:
            return parse_json_strict(raw)
        except json.JSONDecodeError as exc:
            raise Permanent(f"gemini returned invalid JSON: {exc}") from exc
