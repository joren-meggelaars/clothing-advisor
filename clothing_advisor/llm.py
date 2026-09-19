"""Single entry point for Claude calls: structured outputs, cost ledger, budget guard, error mapping."""
from __future__ import annotations

import logging
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from .usage import BudgetExceeded, CostTracker

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# Models that take adaptive thinking / effort. Haiku 4.5 and older take neither the same way.
_MODERN_PREFIXES = ("claude-sonnet-5", "claude-opus-", "claude-fable-", "claude-sonnet-4-6")


class LlmError(Exception):
    """Any failure talking to Claude; the message is safe to show to the user."""


def _modern(model: str) -> bool:
    return model.startswith(_MODERN_PREFIXES)


class LlmClient:
    def __init__(self, tracker: CostTracker, client: Any | None = None):
        self.tracker = tracker
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                self._client = anthropic.Anthropic()
            except Exception as e:  # missing API key raises at construction
                raise LlmError(f"Claude API is not configured: {e}") from e
        return self._client

    def structured(self, *, purpose: str, model: str, system: Any, messages: list[dict[str, Any]],
                   out_model: type[T], max_tokens: int, thinking: str | None = None,
                   effort: str | None = None) -> T:
        """Call Claude and return a validated `out_model`. thinking: 'off' | 'adaptive' | None (model default)."""
        self.tracker.check()  # raises BudgetExceeded
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(out_model)}
        }
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
        if _modern(model):
            if effort:
                output_config["effort"] = effort
            if thinking == "off":
                kwargs["thinking"] = {"type": "disabled"}
            elif thinking == "adaptive":
                kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = output_config

        try:
            resp = self._get_client().messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LlmError("The Anthropic API key was rejected (check ANTHROPIC_API_KEY).") from e
        except anthropic.RateLimitError as e:
            raise LlmError("Claude is rate limiting requests right now; try again in a minute.") from e
        except anthropic.BadRequestError as e:
            log.error("bad request to Claude: %s", e)
            raise LlmError(f"Claude rejected the request: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LlmError(f"Claude API error ({e.status_code}); try again later.") from e
        except anthropic.APIConnectionError as e:
            raise LlmError("Cannot reach the Claude API (network problem).") from e

        u = resp.usage
        self.tracker.record(
            purpose, model,
            getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0,
            getattr(u, "cache_read_input_tokens", 0) or 0, getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        if resp.stop_reason == "refusal":
            raise LlmError("Claude declined this request.")
        if resp.stop_reason == "max_tokens":
            raise LlmError("Claude's answer was cut off (token limit); try again.")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return out_model.model_validate_json(text)
        except ValidationError as e:
            raise LlmError(f"Claude returned an unexpected structure: {e.error_count()} validation errors") from e


__all__ = ["LlmClient", "LlmError", "BudgetExceeded"]
