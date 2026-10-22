"""The client's statement for closed trades: commissions, success fee, tax, and what is left.

Trades are booked in the order they closed, within the calendar (tax) year:

  * commissions on the buy and the sale, on a per-share schedule like
    Interactive Brokers' (the Alpaca paper account charges none, so on paper
    this is what a real account would have paid);
  * the operator's success fee, a share of new profit only: after a loss it
    is charged again only once the loss has been made back (a high-water mark
    that starts over each year);
  * capital-gains tax on the year's running profit after commissions and fees.
    A loss after a gain gives tax back, as an Israeli broker's tax shield does,
    and what is still unused is shown as the loss left to offset.

Israeli law lets an unused capital loss carry into later years; `carry_losses`
does that. Without it each year starts from zero, as the client asked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from tradesys.core.models import TradeRecord

TAX_RATE = 0.25
SUCCESS_FEE = 0.10


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    per_share: float = 0.005
    minimum: float = 1.0
    max_pct: float = 1.0          # never more than this % of the order's value

    def commission(self, qty: int, price: float) -> float:
        return min(max(self.minimum, self.per_share * qty), self.max_pct / 100 * qty * price)


@dataclass(frozen=True, slots=True)
class Line:
    trade_id: int
    symbol: str
    year: int
    closed_at: str
    qty: int
    entry: float
    exit: float
    gross: float          # (exit - entry) x qty
    commissions: float    # buy + sale
    net: float            # gross - commissions
    fee: float            # success fee charged on this trade
    tax: float            # tax on this trade; negative is tax given back by an offset
    take_home: float      # net - fee - tax
    loss_left: float      # the year's loss still waiting to be offset, after this trade


@dataclass(slots=True)
class Year:
    year: int
    trades: int = 0
    gross: float = 0.0
    commissions: float = 0.0
    net: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    take_home: float = 0.0
    loss_left: float = 0.0
    carried_in: float = 0.0   # loss brought from the year before (carry_losses only)
    lines: list[Line] = field(default_factory=list)


def statement(
    trades: Sequence[TradeRecord],
    *,
    fees: FeeSchedule = FeeSchedule(),
    fee_rate: float = SUCCESS_FEE,
    tax_rate: float = TAX_RATE,
    carry_losses: bool = False,
) -> list[Year]:
    """One entry per calendar year, oldest first, each with its trades."""
    years: list[Year] = []
    closed = sorted((t for t in trades if t.closed_at and t.exit_price is not None),
                    key=lambda t: t.closed_at)
    carry = 0.0
    for trade in closed:
        if not years or years[-1].year != trade.closed_at.year:
            if years and carry_losses:
                carry = min(0.0, -years[-1].loss_left)
            year = Year(trade.closed_at.year, carried_in=-carry)
            years.append(year)
            running, high_water, fees_so_far, tax_so_far = carry, 0.0, 0.0, 0.0
        year = years[-1]

        gross = (trade.exit_price - trade.entry_price) * trade.qty
        commissions = fees.commission(trade.qty, trade.entry_price) + fees.commission(trade.qty, trade.exit_price)
        net = gross - commissions
        running += net
        fee = fee_rate * max(0.0, running - high_water)
        high_water = max(high_water, running)
        fees_so_far += fee
        taxable = running - fees_so_far           # the fee is an expense against the gain
        tax_due = tax_rate * max(0.0, taxable)
        tax = tax_due - tax_so_far
        tax_so_far = tax_due

        line = Line(
            trade.trade_id, trade.symbol, year.year, trade.closed_at.date().isoformat(), trade.qty,
            trade.entry_price, trade.exit_price, gross, commissions, net, fee, tax,
            net - fee - tax, max(0.0, -taxable),
        )
        year.lines.append(line)
        year.trades += 1
        year.gross += gross
        year.commissions += commissions
        year.net += net
        year.fee += fee
        year.tax += tax
        year.take_home += line.take_home
        year.loss_left = line.loss_left
    return years


def line_for(trades: Sequence[TradeRecord], trade_id: int, **options) -> Line | None:
    """The statement line of one trade, as booked among the others."""
    for year in statement(trades, **options):
        for line in year.lines:
            if line.trade_id == trade_id:
                return line
    return None
