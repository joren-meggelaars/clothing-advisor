"""LlmClient.search: the web-search research call (separate from the structured-output path)."""
import json
from types import SimpleNamespace

import pytest

from clothing_advisor.llm import LlmClient, LlmError
from clothing_advisor.usage import SEARCH_USD


def block(**kw):
    return SimpleNamespace(**kw)


def usage(in_tok=1000, out_tok=200, searches=0, cache_read=0, cache_write=0):
    return SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok, cache_read_input_tokens=cache_read,
                           cache_creation_input_tokens=cache_write,
                           server_tool_use=SimpleNamespace(web_search_requests=searches) if searches else None)


class FakeSearchAnthropic:
    """Returns queued responses (each a list of content blocks + usage + stop_reason); records every request."""

    def __init__(self):
        self.calls = []
        self.responses = []
        self.messages = SimpleNamespace(create=self._create)

    def queue(self, content, usage_=None, stop_reason="end_turn"):
        self.responses.append(SimpleNamespace(content=content, usage=usage_ or usage(), stop_reason=stop_reason))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


@pytest.fixture
def fake():
    return FakeSearchAnthropic()


@pytest.fixture
def llm(ctx):
    return ctx.llm


def test_search_returns_the_answer_after_the_last_search_and_records_cost(llm, fake, ctx):
    fake.queue([
        block(type="text", text="I'll look this up."),
        block(type="server_tool_use", id="t1", name="web_search", input={"query": "smart casual trends"}),
        block(type="web_search_tool_result", tool_use_id="t1", content=[
            block(url="https://zalando.nl/x", title="Zalando trends", page_age="2026")]),
        block(type="text", text="Camel overshirts are trending.", citations=[
            block(url="https://zalando.nl/x", title="Zalando trends")]),
    ], usage_=usage(in_tok=500, out_tok=100, searches=1))

    out = llm.search(purpose="shop_brief", model="claude-sonnet-5", system="sys", prompt="research",
                     max_tokens=2000, max_uses=5)

    assert out.text == "Camel overshirts are trending."
    assert out.sources == [{"url": "https://zalando.nl/x", "title": "Zalando trends"}]
    assert out.searches == 1 and not out.truncated
    call = fake.calls[0]
    assert call["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    assert call["thinking"] == {"type": "disabled"}
    spend = ctx.tracker.spend()
    assert spend > 0
    # one search at $10/1000 plus tokens, converted with the configured rate
    tokens_usd = 500 / 1e6 * 2.0 + 100 / 1e6 * 10.0
    assert spend == pytest.approx((tokens_usd + SEARCH_USD) * ctx.cfg.usd_eur, rel=1e-6)


def test_search_with_no_results_still_returns_claudes_text(llm, fake):
    fake.queue([
        block(type="server_tool_use", id="t1", name="web_search", input={"query": "x"}),
        block(type="web_search_tool_result", tool_use_id="t1", content=[]),   # a search that matched nothing
        block(type="text", text="I found nothing specific, but in general..."),
    ], usage_=usage(searches=1))

    out = llm.search(purpose="p", model="claude-sonnet-5", system="s", prompt="q", max_tokens=100, max_uses=3)
    assert out.text == "I found nothing specific, but in general..." and out.sources == []


def test_search_tool_error_is_not_billed_and_does_not_crash(llm, fake):
    fake.queue([
        block(type="server_tool_use", id="t1", name="web_search", input={"query": "x"}),
        block(type="web_search_tool_result", tool_use_id="t1",
             content=block(type="web_search_tool_result_error", error_code="max_uses_exceeded")),
        block(type="text", text="I could not search further, but here is what I know."),
    ], usage_=usage(searches=0))   # a failed search is not counted/billed per the API's own rule

    out = llm.search(purpose="p", model="claude-sonnet-5", system="s", prompt="q", max_tokens=100, max_uses=1)
    assert out.searches == 0 and "could not search" in out.text


def test_search_follows_a_paused_turn_and_sums_searches_across_both_calls(llm, fake):
    first_content = [
        block(type="server_tool_use", id="t1", name="web_search", input={"query": "a"}),
        block(type="web_search_tool_result", tool_use_id="t1", content=[block(url="https://a.example", title="A")]),
    ]
    fake.queue(first_content, usage_=usage(searches=1), stop_reason="pause_turn")
    fake.queue([
        block(type="server_tool_use", id="t2", name="web_search", input={"query": "b"}),
        block(type="web_search_tool_result", tool_use_id="t2", content=[block(url="https://b.example", title="B")]),
        block(type="text", text="Combined finding."),
    ], usage_=usage(searches=1))

    out = llm.search(purpose="p", model="claude-sonnet-5", system="s", prompt="q", max_tokens=100, max_uses=5)

    assert out.searches == 2 and out.text == "Combined finding."
    assert {s["url"] for s in out.sources} == {"https://a.example", "https://b.example"}
    second_messages = fake.calls[1]["messages"]
    assert second_messages[0]["role"] == "user"
    assert second_messages[1]["role"] == "assistant"       # the paused turn's content sent back, per the API's contract


def test_search_refusal_raises(llm, fake):
    fake.queue([block(type="text", text="")], stop_reason="refusal")
    with pytest.raises(LlmError, match="declined"):
        llm.search(purpose="p", model="claude-sonnet-5", system="s", prompt="q", max_tokens=100, max_uses=1)


def test_search_with_empty_answer_raises_a_clear_error(llm, fake):
    fake.queue([block(type="server_tool_use", id="t1", name="web_search", input={"query": "x"})], usage_=usage(searches=1))
    with pytest.raises(LlmError, match="came back empty"):
        llm.search(purpose="p", model="claude-sonnet-5", system="s", prompt="q", max_tokens=100, max_uses=1)


def test_search_passes_domain_filter_and_user_location(llm, fake):
    fake.queue([block(type="text", text="ok")])
    llm.search(purpose="p", model="claude-sonnet-5", system="s", prompt="q", max_tokens=100, max_uses=2,
              allowed_domains=["zalando.nl"], user_location={"type": "approximate", "country": "NL"})
    tool = fake.calls[0]["tools"][0]
    assert tool["allowed_domains"] == ["zalando.nl"] and tool["user_location"]["country"] == "NL"


def test_search_checks_the_budget_before_calling(llm, fake, ctx, monkeypatch):
    from clothing_advisor.usage import BudgetExceeded
    monkeypatch.setattr(ctx.tracker, "check", lambda: (_ for _ in ()).throw(BudgetExceeded("no budget left")))
    with pytest.raises(BudgetExceeded):
        llm.search(purpose="p", model="claude-sonnet-5", system="s", prompt="q", max_tokens=100, max_uses=1)
    assert fake.calls == []
