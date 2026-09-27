"""Single entry point for Claude calls: structured outputs, cost ledger, budget guard, error mapping."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from .usage import SEARCH_USD, BudgetExceeded, CostTracker

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# Models that take adaptive thinking / effort. Haiku 4.5 and older take neither the same way.
_MODERN_PREFIXES = ("claude-sonnet-5", "claude-opus-", "claude-fable-", "claude-sonnet-4-6")


SEARCH_TOOL = "web_search_20250305"   # the basic version: every result reaches the model, no code execution involved
MAX_CONTINUATIONS = 3                 # a long search turn may be paused (stop_reason pause_turn) and must be resumed


class LlmError(Exception):
    """Any failure talking to Claude; the message is safe to show to the user."""


@dataclass
class SearchOutcome:
    text: str
    sources: list[dict[str, str]] = field(default_factory=list)
    searches: int = 0
    truncated: bool = False


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute of an SDK object or key of a dict (the fakes in the tests use both)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _dump(block: Any) -> Any:
    """A content block (or anything nested inside one) as plain data, to send it back unchanged when a paused
    turn is resumed."""
    if isinstance(block, (str, int, float, bool, type(None))):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    if isinstance(block, dict):
        return {k: _dump(v) for k, v in block.items()}
    if isinstance(block, (list, tuple)):
        return [_dump(v) for v in block]
    return {k: _dump(v) for k, v in vars(block).items()}


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

    def _create(self, kwargs: dict[str, Any]) -> Any:
        try:
            return self._get_client().messages.create(**kwargs)
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

    def search(self, *, purpose: str, model: str, system: str, prompt: str, max_tokens: int, max_uses: int,
               allowed_domains: list[str] | None = None, user_location: dict[str, str] | None = None,
               effort: str | None = None) -> SearchOutcome:
        """Let Claude research with the web search tool and return its written answer plus the pages it found.
        Costs are recorded per call: the tokens plus SEARCH_USD for every search that was really run."""
        self.tracker.check()
        tool: dict[str, Any] = {"type": SEARCH_TOOL, "name": "web_search", "max_uses": max_uses}
        if allowed_domains:
            tool["allowed_domains"] = allowed_domains
        if user_location:
            tool["user_location"] = user_location
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system, "tools": [tool]}
        if _modern(model):
            kwargs["thinking"] = {"type": "disabled"}      # the searches are the work; thinking would only add cost
            if effort:
                kwargs["output_config"] = {"effort": effort}
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        sources: dict[str, str] = {}
        total_searches, text, truncated = 0, "", False
        for _ in range(MAX_CONTINUATIONS + 1):
            resp = self._create({**kwargs, "messages": messages})
            u = resp.usage
            server = _get(u, "server_tool_use")
            searches = int(_get(server, "web_search_requests", 0) or 0) if server is not None else 0
            total_searches += searches
            self.tracker.record(
                purpose, model, _get(u, "input_tokens", 0) or 0, _get(u, "output_tokens", 0) or 0,
                _get(u, "cache_read_input_tokens", 0) or 0, _get(u, "cache_creation_input_tokens", 0) or 0,
                extra_usd=searches * SEARCH_USD,
            )
            content = list(resp.content)
            last_result, pieces = -1, []
            for n, block in enumerate(content):
                kind = _get(block, "type")
                if kind == "web_search_tool_result":
                    last_result = n
                    found = _get(block, "content")
                    for r in found if isinstance(found, list) else []:          # an error is one object, not a list
                        if _get(r, "url"):
                            sources.setdefault(str(_get(r, "url")), str(_get(r, "title", "") or ""))
                elif kind == "text":
                    pieces.append((n, str(_get(block, "text", "") or "")))
                    for cite in _get(block, "citations") or []:
                        if _get(cite, "url"):
                            sources.setdefault(str(_get(cite, "url")), str(_get(cite, "title", "") or ""))
            # The answer is what Claude writes after its last search; before that it only narrates what it is doing.
            answer = "".join(t for n, t in pieces if n > last_result).strip() or "".join(t for _, t in pieces).strip()
            text = (text + "\n\n" + answer).strip() if text and answer else (answer or text)
            if resp.stop_reason == "refusal":
                raise LlmError("Claude declined this request.")
            if resp.stop_reason == "pause_turn":
                messages = messages + [{"role": "assistant", "content": [_dump(b) for b in content]}]
                continue
            truncated = resp.stop_reason == "max_tokens"
            break
        if not text:
            raise LlmError("The research came back empty; try again.")
        return SearchOutcome(text=text, sources=[{"url": u, "title": t} for u, t in sources.items()],
                             searches=total_searches, truncated=truncated)

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

        resp = self._create(kwargs)

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
