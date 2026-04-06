"""Ollama local LLM client.

Uses only stdlib urllib — no new dependencies required.
Ollama must be running locally (default: http://localhost:11434).

Usage:
    client = OllamaClient()
    if client.is_available():
        text = client.generate("llama3.2:3b", "Summarize: ...")
        vec = client.embed("nomic-embed-text", "some text")
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "30"))


class OllamaClient:
    """Thin wrapper around the Ollama REST API."""

    def __init__(self, base_url: str = _DEFAULT_BASE_URL):
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, payload: dict, timeout: int = _TIMEOUT) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            raise ConnectionError(f"Ollama unreachable at {self.base_url}: {e}") from e

    def _get(self, path: str, timeout: int = _TIMEOUT) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            raise ConnectionError(f"Ollama unreachable at {self.base_url}: {e}") from e

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if Ollama is reachable."""
        try:
            self._get("/api/tags", timeout=3)
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        """Return list of locally available model names."""
        try:
            data = self._get("/api/tags")
            return [m["name"] for m in data.get("models", [])]
        except Exception as e:
            log.debug("Ollama list_models error: %s", e)
            return []

    def generate(self, model: str, prompt: str,
                 system: Optional[str] = None,
                 temperature: float = 0.2) -> str:
        """Single-turn text generation. Returns the response string."""
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system
        try:
            result = self._post("/api/generate", payload, timeout=120)
            return result.get("response", "").strip()
        except Exception as e:
            log.warning("Ollama generate error (model=%s): %s", model, e)
            return ""

    def chat(self, model: str, messages: list[dict],
             temperature: float = 0.2) -> str:
        """Multi-turn chat. messages = [{"role": "user"|"assistant", "content": "..."}]"""
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        try:
            result = self._post("/api/chat", payload, timeout=120)
            return result.get("message", {}).get("content", "").strip()
        except Exception as e:
            log.warning("Ollama chat error (model=%s): %s", model, e)
            return ""

    def embed(self, model: str, text: str) -> list[float]:
        """Generate a text embedding vector. Returns [] on failure."""
        payload = {"model": model, "input": text}
        try:
            result = self._post("/api/embed", payload, timeout=60)
            # Ollama /api/embed returns {"embeddings": [[...]], ...}
            embeddings = result.get("embeddings")
            if embeddings and isinstance(embeddings, list) and embeddings[0]:
                return embeddings[0]
            return []
        except Exception as e:
            log.debug("Ollama embed error (model=%s): %s", model, e)
            return []

    def pull_model(self, model: str) -> bool:
        """Pull a model from the Ollama library. Blocks until complete."""
        payload = {"name": model, "stream": False}
        try:
            self._post("/api/pull", payload, timeout=600)
            return True
        except Exception as e:
            log.warning("Ollama pull error (model=%s): %s", model, e)
            return False


# Module-level singleton
_client: Optional[OllamaClient] = None


def get_client() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client
