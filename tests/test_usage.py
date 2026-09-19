import pytest

from clothing_advisor.usage import BudgetExceeded, cost_usd


def test_cost_math_sonnet():
    assert cost_usd("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    # cache reads cost 10% of input, cache writes 125%
    assert cost_usd("claude-sonnet-5", 0, 0, cache_read=1_000_000, cache_write=1_000_000) == pytest.approx(0.2 + 2.5)


def test_unknown_model_is_priced_conservatively():
    assert cost_usd("claude-mystery", 1_000_000, 0) == pytest.approx(10.0)


def test_record_and_month_total(ctx):
    eur = ctx.tracker.record("advice", "claude-sonnet-5", 12000, 1500)
    assert eur == pytest.approx((12000 * 2 + 1500 * 10) / 1e6 * ctx.cfg.usd_eur)
    assert ctx.tracker.spend() == pytest.approx(eur)


def test_alerts_fire_once_per_threshold_and_enforce_blocks(ctx):
    sent = []
    ctx.notifier.channels = lambda: ["test"]
    ctx.notifier.send = lambda title, body: (sent.append(title) or (["test"], []))
    ctx.db.set_setting("budget_eur", "1.00")

    ctx.tracker.record("advice", "claude-sonnet-5", 0, 43_500)  # ~EUR 0.40 -> under 80%
    assert sent == []
    ctx.tracker.record("advice", "claude-sonnet-5", 0, 43_500)  # ~EUR 0.80 -> 80%
    assert len(sent) == 1 and "80%" in sent[0]
    ctx.tracker.record("advice", "claude-sonnet-5", 0, 1_000)   # still between 80 and 100
    assert len(sent) == 1
    ctx.tracker.check()  # not blocked yet
    ctx.tracker.record("advice", "claude-sonnet-5", 0, 30_000)  # over 100%
    assert len(sent) == 2 and "100%" in sent[1]
    with pytest.raises(BudgetExceeded):
        ctx.tracker.check()


def test_alert_retried_when_delivery_fails(ctx):
    calls = []
    ctx.notifier.channels = lambda: ["test"]
    ctx.notifier.send = lambda t, b: (calls.append(t) or ([], ["boom"]))
    ctx.db.set_setting("budget_eur", "0.10")
    ctx.tracker.record("advice", "claude-sonnet-5", 0, 50_000)
    ctx.tracker.record("advice", "claude-sonnet-5", 0, 10)
    assert len(calls) >= 3  # 80% and 100% both retried on the next call


def test_no_channel_marks_alert_sent_so_banner_is_the_signal(ctx):
    ctx.db.set_setting("budget_eur", "0.10")
    ctx.tracker.record("advice", "claude-sonnet-5", 0, 50_000)
    assert ctx.tracker.fired_alerts() == [80, 100]
