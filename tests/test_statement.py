from __future__ import annotations

from datetime import datetime, timezone

from pytest import approx

from tradesys.clients.statement import FeeSchedule, line_for, statement
from tradesys.core.enums import SetupType, Side
from tradesys.core.models import TradeRecord

NO_FEES = FeeSchedule(per_share=0.0, minimum=0.0)


def trade(i: int, entry: float, exit_: float, qty: int = 10, when: str = "2026-03-01") -> TradeRecord:
    closed = datetime.fromisoformat(when).replace(hour=20, tzinfo=timezone.utc)
    return TradeRecord(i, "tal_dahan", f"S{i}", SetupType.TREND_CONTINUATION, Side.BUY, qty, entry,
                       entry * 0.9, closed, exit_price=exit_, closed_at=closed, realized_pnl=(exit_ - entry) * qty)


def test_a_win_pays_fee_and_tax_and_a_later_loss_gives_tax_back():
    # +1,000 then -400 then +200, no commissions.
    trades = [trade(1, 100, 200), trade(2, 100, 60, when="2026-04-01"), trade(3, 100, 120, when="2026-05-01")]
    [year] = statement(trades, fees=NO_FEES)
    win, loss, back = year.lines

    assert (win.fee, win.tax, win.take_home) == approx((100, 225, 675))      # 10% fee, 25% of 900
    assert loss.fee == 0 and loss.tax == approx(-100)                        # 25% of the 400 loss back
    assert loss.loss_left == 0                                               # absorbed by the earlier gain
    # Running profit 800 -> 1,000: no new fee until it passes the 1,000 high-water mark.
    assert back.fee == 0 and back.tax == approx(50)
    assert year.tax == approx(0.25 * (800 - 100))
    assert year.take_home == approx(800 - 100 - 175)


def test_a_loss_first_is_offset_before_any_tax_or_fee():
    trades = [trade(1, 100, 70), trade(2, 100, 150, when="2026-06-01")]     # -300, then +500
    [year] = statement(trades, fees=NO_FEES)
    loss, win = year.lines

    assert loss.loss_left == approx(300) and loss.tax == 0
    assert win.fee == approx(20)                                             # 10% of the 200 above zero
    assert win.tax == approx(0.25 * (200 - 20))
    assert win.loss_left == 0


def test_each_year_starts_over_unless_losses_are_carried():
    trades = [trade(1, 100, 50, when="2025-11-01"), trade(2, 100, 150, when="2026-02-01")]   # -500, +500

    reset = statement(trades, fees=NO_FEES)
    carried = statement(trades, fees=NO_FEES, carry_losses=True)

    assert reset[0].loss_left == approx(500)
    assert reset[1].tax == approx(0.25 * 450)                               # fee 50, tax on 450
    assert carried[1].carried_in == approx(500)
    assert carried[1].tax == 0 and carried[1].fee == 0                     # the old loss absorbs it


def test_commissions_follow_the_per_share_schedule():
    fees = FeeSchedule()
    assert fees.commission(100, 50.0) == approx(1.0)        # minimum
    assert fees.commission(1000, 50.0) == approx(5.0)       # half a cent a share
    assert fees.commission(1000, 0.5) == approx(5.0)        # capped at 1% of 500
    line = line_for([trade(1, 100, 110, qty=1000)], 1)
    assert line.commissions == approx(10.0) and line.net == approx(10_000 - 10)


def test_the_fee_follows_the_admin_billing_setting(tmp_path):
    from tradesys.clients.statement import SUCCESS_FEE, fee_rate

    assert fee_rate(tmp_path) == SUCCESS_FEE
    (tmp_path / "billing.json").write_text('{"profit_share_pct": 15.0}', encoding="utf-8")
    assert fee_rate(tmp_path) == 0.15
