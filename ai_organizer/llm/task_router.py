"""Multi-LLM task router.

Maps high-level task types to the best locally available Ollama model,
with automatic fallback chains. All calls degrade gracefully — if Ollama
is not running or no suitable model is installed, returns None so callers
can fall back to Python-only logic.

Usage:
    router = TaskRouter()
    answer = router.run_task("intent_classification", "how many PDFs?")
    # Returns None if Ollama unavailable — caller uses Python fallback
"""

import logging
from typing import Optional

from .ollama_client import get_client

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Task → preferred model chains
# ---------------------------------------------------------------------------
# Each entry is an ordered list of model name prefixes to try.
# The router picks the first one that is installed locally.

_TASK_MODEL_CHAIN: dict[str, list[str]] = {
    # Short intent classification — small fast model preferred
    "intent_classification": ["llama3.2:3b", "llama3.2:1b", "phi3:mini", "phi3"],
    # Metadata / file summarization
    "metadata_summary": ["llama3.2:3b", "llama3.2:1b", "phi3:mini", "phi3", "mistral:7b"],
    # Longer document understanding
    "document_understanding": ["llama3.2:8b", "llama3.2:3b", "mistral:7b", "llama3.2"],
    # Image description — requires a vision-capable model
    "image_description": ["llava:7b", "llava:13b", "llava"],
    # Cluster / person naming for facial recognition
    "face_cluster_naming": ["llama3.2:3b", "llama3.2:1b", "phi3:mini"],
    # Code analysis
    "code_analysis": ["codellama:7b", "codellama", "llama3.2:8b", "llama3.2:3b"],
    # Agent narrative generation (workflow summaries)
    "agent_narrative": ["llama3.2:3b", "llama3.2:1b", "mistral:7b", "phi3:mini"],
}

# System prompts per task type
_SYSTEM_PROMPTS: dict[str, str] = {
    "intent_classification": (
        "You are a file system query classifier. "
        "Classify the user's question into exactly one of these intents: "
        "count, search, compare, describe, find_related, list_types, storage_usage. "
        "Reply with ONLY the intent label — no explanation."
    ),
    "metadata_summary": (
        "You are a concise file metadata summarizer. "
        "Summarize the provided metadata in 1-2 sentences."
    ),
    "document_understanding": (
        "You are a document analyst. Answer questions about the document content provided."
    ),
    "image_description": (
        "Describe this image concisely in 1-2 sentences, focusing on its content and category "
        "(photo, screenshot, diagram, receipt, art, etc.)."
    ),
    "face_cluster_naming": (
        "Given a list of filenames that contain photos of the same person, "
        "suggest a short descriptive label (first name or role, e.g. 'Alice' or 'Unknown Person 1'). "
        "Reply with ONLY the label."
    ),
    "code_analysis": (
        "You are a senior software engineer. Analyze the provided code snippet concisely."
    ),
    "agent_narrative": (
        "You are a helpful assistant summarizing the results of an automated workflow. "
        "Write a clear, friendly 2-3 sentence summary of the results."
    ),
}


class TaskRouter:
    """Routes tasks to the best available local Ollama model."""

    def __init__(self):
        self._client = get_client()
        self._available_models: Optional[list[str]] = None

    def _get_available(self) -> list[str]:
        """Cache available model list (refresh on each TaskRouter instance)."""
        if self._available_models is None:
            self._available_models = self._client.list_models()
        return self._available_models

    def get_model_for_task(self, task: str) -> Optional[str]:
        """Return the best locally available model for the given task, or None."""
        chain = _TASK_MODEL_CHAIN.get(task, [])
        available = self._get_available()
        for preferred in chain:
            for installed in available:
                # Match by prefix so "llama3.2:3b" matches "llama3.2:3b-instruct-q4_K_M"
                if installed.startswith(preferred) or preferred.startswith(installed.split(":")[0]):
                    return installed
        # Final fallback: any available model (except vision models for non-vision tasks)
        if available and task != "image_description":
            return available[0]
        return None

    def run_task(self, task: str, prompt: str,
                 context: Optional[dict] = None) -> Optional[str]:
        """Run a task prompt through the best available model.

        Returns the model's response string, or None if Ollama is unavailable
        or no suitable model is installed.
        """
        if not self._client.is_available():
            log.debug("Ollama not available — skipping task '%s'", task)
            return None

        model = self.get_model_for_task(task)
        if not model:
            log.debug("No model available for task '%s'", task)
            return None

        system = _SYSTEM_PROMPTS.get(task)

        # Prepend context if provided
        full_prompt = prompt
        if context:
            ctx_str = "\n".join(f"{k}: {v}" for k, v in context.items())
            full_prompt = f"Context:\n{ctx_str}\n\nQuestion: {prompt}"

        response = self._client.generate(model, full_prompt, system=system)
        if response:
            log.debug("Task '%s' via model '%s': %d chars", task, model, len(response))
        return response or None

    def get_status(self) -> dict:
        """Return diagnostic info for the /api/llm/status endpoint."""
        available = self._client.is_available()
        models = self._client.list_models() if available else []
        task_assignments = {}
        if available:
            for task in _TASK_MODEL_CHAIN:
                task_assignments[task] = self.get_model_for_task(task)
        return {
            "ollama_available": available,
            "base_url": self._client.base_url,
            "models_installed": models,
            "task_assignments": task_assignments,
        }


# Module-level singleton
_router: Optional[TaskRouter] = None


def get_router() -> TaskRouter:
    global _router
    if _router is None:
        _router = TaskRouter()
    return _router
