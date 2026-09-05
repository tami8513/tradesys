from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SetupType(str, Enum):
    BREAKOUT = "breakout"
    PULLBACK = "pullback"
    OVERSOLD_REVERSAL = "oversold_reversal"
    TREND_CONTINUATION = "trend_continuation"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"


class OrderStatus(str, Enum):
    ACCEPTED = "accepted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELED = "canceled"


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIME_STOP = "time_stop"
    BROKER_CLOSED = "broker_closed"   # the position left the broker without us asking
    MANUAL = "manual"


class MemoryKind(str, Enum):
    HUMAN_FEEDBACK = "human_feedback"
    TRADE_POSTMORTEM = "trade_postmortem"
    SESSION_LESSON = "session_lesson"


class DirectiveAction(str, Enum):
    """Machine-enforceable intent extracted from free-text operator feedback."""

    PRIORITIZE = "prioritize"
    DEPRIORITIZE = "deprioritize"
    AVOID = "avoid"      # hard veto — enforced deterministically, never by the LLM
    NOTE = "note"        # advisory only, surfaced as RAG context


class DirectiveScope(str, Enum):
    SYMBOL = "symbol"
    SETUP = "setup"
    GLOBAL = "global"
