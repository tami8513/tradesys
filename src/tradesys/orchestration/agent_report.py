"""Operator-only report card for each agent, built from what the journal recorded.

Every grade is computed from facts — closed trades, logged decisions, order
statuses — and an agent with too little history is reported as such rather than
graded. A confident score drawn from three trades would mislead exactly the
person using this report to decide what to change.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from tradesys.agents.offense import CONVICTION, PROVEN_TRADES, SetupEdge
from tradesys.clients.profile import RiskProfile
from tradesys.core.enums import Side
from tradesys.core.models import TradeRecord
from tradesys.orchestration.track_record import Outcome

MIN_TRADES = 5
# Joins a rejection's reasons in the event log. Not "; ": reasons contain it.
REASON_SEP = " || "
REPLAY_MIN_TRADES = 10    # fewer trades than this is noise, not a verdict on a setup
GAP_THROUGH_STOP = 0.10
CRASH = 0.30
SIM_PATHS = 1000
SIM_TRADES = 100
SETUP_NAMES = {
    "breakout": "פריצה",
    "pullback": "תיקון במגמה",
    "oversold_reversal": "היפוך ממכירת יתר",
    "trend_continuation": "המשך מגמה",
}
EXIT_NAMES = {
    "stop_loss": "סטופ",
    "take_profit": "יעד",
    "time_stop": "זמן",
    "broker_closed": "נסגר אצל הברוקר",
    "earnings_trim": "הקטנה לפני דוח רבעוני",
    "manual": "ידני",
}


@dataclass(slots=True)
class AgentCard:
    key: str
    name: str
    role: str
    status: str = "no_data"            # good | watch | bad | no_data
    kpis: list[tuple[str, str]] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)      # where it should improve
    strengths: list[str] = field(default_factory=list)     # what works
    verdict: str = ""                                      # one line for the overview


@dataclass(frozen=True, slots=True)
class Book:
    """One tenant's live exposure, for the stress tests."""

    client_id: str
    equity: float
    open_trades: Sequence[TradeRecord]
    risk: RiskProfile
    r_history: Sequence[float]           # every closed trade's R-multiple, all time


@dataclass(frozen=True, slots=True)
class ReportInput:
    trades: Sequence[TradeRecord]        # closed trades in the period
    events: Sequence[dict[str, Any]]     # agent_events rows in the period
    order_statuses: Sequence[str]
    active_directives: int
    memory_records: int
    books: Sequence[Book] = ()
    replays: Sequence[tuple[str, dict[str, Any]]] = ()   # (client_id, latest historical test)
    scout_outcomes: Sequence[Outcome] = ()                # the scout's shortlists, priced again
    analysis_outcomes: Sequence[Outcome] = ()             # full analyses, priced again
    research_outcomes: Sequence[Outcome] = ()             # the researcher's theme stocks, priced again
    ops: Sequence[Ops] = ()                               # per client: phone channel and alerts


@dataclass(frozen=True, slots=True)
class Ops:
    client_id: str
    phone_connected: bool
    open_alerts: int
    questions_asked: int
    questions_answered: int


def build_report(data: ReportInput) -> list[AgentCard]:
    cards = [
        _scout(data),
        _offense(data),
        _risk(data),
        _execution(data),
        _exits(data),
        _learning(data),
        _efficiency(data),
        _stress(data),
        _index(data),
        _replay(data),
        _hunter(data),
        _talent_scout(data),
        _money_flow(data),
        _analyst(data),
        _researcher(data),
        _alerts(data),
    ]
    for card in cards:
        card.verdict = card.verdict or DEFAULT_VERDICTS[card.status]
    return [_head(cards), *cards]


def _events(data: ReportInput, agent: str, kind: str | None = None) -> list[dict[str, Any]]:
    return [
        e for e in data.events
        if e["agent"] == agent and (kind is None or e["kind"] == kind)
    ]


DEFAULT_VERDICTS = {
    "good": "עובד טוב",
    "watch": "עובד, אבל יש מה לשפר",
    "bad": "דורש טיפול",
    "no_data": "אין עדיין מספיק נתונים לשפוט",
}
ANALYST_MIN_CALLS = 5


def _avg(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _calls_kpis(card: AgentCard, label: str, results: Sequence[Outcome]) -> None:
    card.kpis.append((
        f"{label}: תשואה ממוצעת מאז ההמלצה",
        f"{_avg([o.ret for o in results]):+.1f}% (המדד {_avg([o.bench for o in results]):+.1f}%)",
    ))
    card.kpis.append((f"{label}: ניצחו את המדד", f"{sum(o.beat for o in results)} מתוך {len(results)}"))


def _judge_calls(card: AgentCard, results: Sequence[Outcome], what: str) -> None:
    """Status, verdict and advice from how the calls did against the index."""
    if len(results) < ANALYST_MIN_CALLS:
        card.verdict = f"עוד מוקדם: {len(results)} המלצות בנות שבוע ומעלה, צריך לפחות {ANALYST_MIN_CALLS}"
        return
    edge = _avg([o.ret for o in results]) - _avg([o.bench for o in results])
    card.verdict = f"{what} {'הקדימו' if edge >= 0 else 'פיגרו אחרי'} את המדד ב-{abs(edge):.1f}%"
    if edge >= 1:
        card.status = "good"
        card.strengths.append(f"{what} הקדימו את המדד ב-{edge:.1f}% בממוצע מאז ההמלצה.")
    elif edge >= -1:
        card.status = "watch"
        card.findings.append(f"{what} עשו בערך כמו המדד ({edge:+.1f}%). עדיין לא מוסיף ערך מעבר להחזקת המדד.")
    else:
        card.status = "bad"
        card.findings.append(
            f"{what} פיגרו אחרי המדד ב-{-edge:.1f}%. כדאי לבחון את המשקלים של הציון, "
            "ולא לפעול לפי ההמלצות עד שזה משתפר."
        )


def _avg_r(trades: Sequence[TradeRecord]) -> float:
    return sum(t.r_multiple() for t in trades) / len(trades) if trades else 0.0


def _scout(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "scout",
        "סוכן כניסה",
        "סורק את רשימת המניות בכל סבב ומזהה מתי להיכנס: פריצה, תיקון במגמה, "
        "המשך מגמה או היפוך ממכירת יתר. כל רעיון עובר לשומר הסיכון לפני שהוא הופך לעסקה.",
    )
    found = _events(data, "scout", "candidate")
    approved = _events(data, "risk", "approved")
    card.kpis.append(("הזדמנויות שזוהו", str(len(found))))
    if found:
        card.kpis.append(("אושרו לביצוע", f"{len(approved) / len(found):.0%}"))

    by_setup: dict[str, list[TradeRecord]] = {}
    for trade in data.trades:
        by_setup.setdefault(trade.setup.value, []).append(trade)
    for setup, trades in sorted(by_setup.items()):
        name = SETUP_NAMES.get(setup, setup)
        avg = _avg_r(trades)
        card.kpis.append((f"{name}: תוחלת", f"{avg:+.2f}R ({len(trades)} עסקאות)"))
        if len(trades) >= MIN_TRADES and avg < 0:
            card.findings.append(
                f"התבנית '{name}' מפסידה: ממוצע {avg:+.2f}R על {len(trades)} עסקאות. "
                "שקול לחסום אותה בפרופיל או לצמצם אותה לניירות שבהם היא עובדת."
            )
        elif len(trades) >= MIN_TRADES and avg >= 0.5:
            card.strengths.append(
                f"התבנית '{name}' היא החזקה בתיק: ממוצע {avg:+.2f}R. "
                "כדאי לשקול להרחיב אותה לניירות נוספים ברשימת המעקב."
            )

    if data.events and not found:
        card.findings.append(
            "לא זוהו הזדמנויות בתקופה. אם השוק לא במגמת ירידה, כדאי להרחיב את רשימת המעקב."
        )
    if len(data.trades) >= MIN_TRADES:
        overall = _avg_r(data.trades)
        card.status = "good" if overall >= 0.2 else "watch" if overall >= 0 else "bad"
    return card


def _reason_key(reason: str) -> str:
    """Group 'qty 120 exceeds cap 80'-style reasons by their wording, not numbers."""
    words = [w for w in reason.split() if not any(ch.isdigit() for ch in w)]
    return " ".join(words[:6]) or reason


def _risk(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "risk",
        "שומר הסיכון",
        "מנוע כללים קשיח, בלי בינה מלאכותית ובלי אקראיות. מאשר או חוסם כל עסקה לפי "
        "מגבלות ההפסד, רצפת הקרן, דוחות קרובים ומצב השוק. הוא היחיד שמותר לו לבנות פקודת כניסה.",
    )
    approved = _events(data, "risk", "approved")
    rejected = _events(data, "risk", "rejected")
    halts = _events(data, "risk", "halted")
    card.kpis += [
        ("אושרו", str(len(approved))),
        ("נחסמו", str(len(rejected))),
        ("סבבים שנעצרו", str(len(halts))),
    ]
    reasons = Counter(
        _reason_key(part)
        for e in rejected
        for part in e["detail"].split(REASON_SEP)
        if part
    )
    for reason, count in reasons.most_common(3):
        card.kpis.append((f"סיבת חסימה: {reason}", str(count)))
    if rejected and reasons:
        top, count = reasons.most_common(1)[0]
        if count / len(rejected) > 0.5:
            card.findings.append(
                f"רוב החסימות ({count} מתוך {len(rejected)}) מאותה סיבה: '{top}'. "
                "אם זו מגבלה שמונעת עסקאות טובות, זה המקום לבדוק אותה. אם היא מגינה, היא עושה את עבודתה."
            )
    if halts:
        card.findings.append(
            f"המסחר נעצר {len(halts)} פעמים על ידי מגבלות ההפסד. כדאי לעבור על העסקאות שהובילו לעצירה."
        )
    if approved or rejected:
        card.strengths.append(
            f"בדק {len(approved) + len(rejected)} רעיונות ואישר רק {len(approved)}: כל עסקה עברה "
            "את כל מגבלות ההפסד, הפיזור והדוחות."
        )
    if approved or rejected or halts:
        card.status = "watch" if halts else "good"
    return card


def _execution(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "execution",
        "סוכן ביצוע",
        "שולח לברוקר רק פקודות שאושרו, עם מזהה ייחודי שמונע שליחה כפולה, "
        "ומחכה לביצוע בפועל לפני שהעסקה נרשמת.",
    )
    statuses = Counter(data.order_statuses)
    total = sum(statuses.values())
    card.kpis.append(("פקודות אחרונות שנבדקו", str(total)))
    if total:
        filled = statuses.get("filled", 0)
        rate = filled / total
        card.kpis += [
            ("בוצעו", f"{rate:.0%}"),
            ("נדחו", str(statuses.get("rejected", 0))),
            ("בוטלו", str(statuses.get("canceled", 0))),
        ]
        card.verdict = f"{rate:.0%} מהפקודות בוצעו"
        if rate >= 0.9:
            card.strengths.append("כמעט כל הפקודות בוצעו, ושום פקודה לא נשלחה פעמיים.")
        if rate < 0.7:
            card.findings.append(
                f"רק {rate:.0%} מהפקודות בוצעו. מחיר הגבול כנראה רחוק מדי מהשוק, "
                "או שהברוקר דוחה פקודות. כדאי לבדוק את הסיבות בלוג."
            )
        card.status = "good" if rate >= 0.9 else "watch" if rate >= 0.7 else "bad"
    return card


def _exits(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "monitor",
        "סוכן יציאה",
        "שומר על כל פוזיציה פתוחה בכל סבב: סטופ, יעד, סטופ נגרר שרק עולה, והחזקה מעבר ליעד "
        "כשהמגמה נמשכת. יציאה תמיד מותרת, גם כשהמסחר עצור.",
    )
    trades = data.trades
    reasons = Counter(t.exit_reason or "unknown" for t in trades)
    for reason, count in reasons.most_common():
        card.kpis.append((f"יציאות ב{EXIT_NAMES.get(reason, 'סיבה לא ידועה')}", str(count)))
    trailed_wins = [
        t for t in trades if t.exit_reason == "stop_loss" and (t.realized_pnl or 0) > 0
    ]
    if trailed_wins:
        card.strengths.append(f"הסטופ הנגרר נעל רווח ב-{len(trailed_wins)} עסקאות שהיו נסגרות אחרת ביעד או בהפסד.")
    card.kpis += [
        ("סטופ נגרר שנעל רווח", str(len(trailed_wins))),
        ("העלאות סטופ", str(len(_events(data, "monitor", "stop_raised")))),
        ("החזקות מעבר ליעד", str(len(_events(data, "monitor", "held_past_target")))),
    ]
    losers = [t for t in trades if (t.realized_pnl or 0) < 0]
    winners = [t for t in trades if (t.realized_pnl or 0) > 0]
    if winners:
        card.kpis.append(("רווח ממוצע", f"{_avg_r(winners):+.2f}R"))
    if losers:
        avg_loss = _avg_r(losers)
        card.kpis.append(("הפסד ממוצע", f"{avg_loss:+.2f}R"))
        if len(losers) >= MIN_TRADES and avg_loss < -1.2:
            card.findings.append(
                f"ההפסד הממוצע ({avg_loss:+.2f}R) גדול מהסיכון שתוכנן (1R). זה סימן לפערי מחיר "
                "מעבר לסטופ, כמו אחרי דוחות או חדשות. כדאי להחמיר את סינון הדוחות או להקטין פוזיציות לילה."
            )
    errors = _events(data, "monitor", "error")
    if errors:
        card.findings.append(f"{len(errors)} תקלות בזמן ניהול פוזיציות. הפרטים בלוג השרת.")
    if len(losers) >= MIN_TRADES:
        avg_loss = _avg_r(losers)
        card.status = "good" if avg_loss >= -1.1 else "watch" if avg_loss >= -1.3 else "bad"
    elif trades:
        card.status = "watch" if errors else "good"
    return card


def _learning(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "reflection",
        "סוכן למידה",
        "אחרי כל עסקה כותב לקח: מה עבד ומה לא. מזהה תבניות שחוזרות על עצמן, והופך משוב "
        "שלך כמו 'הימנע מפריצות' לכלל שהמערכת חייבת לציית לו.",
    )
    lessons = sum(int(e["detail"] or 0) for e in _events(data, "reflection", "lessons"))
    card.kpis += [
        ("לקחים שנכתבו בתקופה", str(lessons)),
        ("לקחים בזיכרון", str(data.memory_records)),
        ("כללים פעילים ממשוב", str(data.active_directives)),
    ]
    if data.trades and lessons == 0:
        card.findings.append(
            "נסגרו עסקאות בלי שנכתבו לקחים חדשים. כדאי לבדוק שהסוכן רץ אחרי כל סגירה."
        )
        card.status = "watch"
    elif lessons:
        card.status = "good"
    return card


def _efficiency(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "efficiency",
        "סוכן יעילות",
        "מודד כמה טוב המערכת מנצלת את הסיכון: תוחלת לעסקה, יחס רווח להפסד, "
        "זמן החזקה ורצפי הפסד. זה המדד שאומר אם האסטרטגיה שווה את הכסף.",
    )
    trades = data.trades
    if not trades:
        return card
    gains = sum(t.realized_pnl or 0 for t in trades if (t.realized_pnl or 0) > 0)
    losses = -sum(t.realized_pnl or 0 for t in trades if (t.realized_pnl or 0) < 0)
    profit_factor = gains / losses if losses else float("inf")
    win_rate = sum(1 for t in trades if (t.realized_pnl or 0) > 0) / len(trades)
    hold_days = [
        (t.closed_at - t.opened_at).total_seconds() / 86400 for t in trades if t.closed_at
    ]
    streak = longest = 0
    for trade in sorted(trades, key=lambda t: t.closed_at or t.opened_at):
        streak = streak + 1 if (trade.realized_pnl or 0) < 0 else 0
        longest = max(longest, streak)
    card.kpis += [
        ("עסקאות סגורות", str(len(trades))),
        ("אחוז הצלחה", f"{win_rate:.0%}"),
        ("יחס רווח/הפסד", "∞" if losses == 0 else f"{profit_factor:.2f}"),
        ("תוחלת לעסקה", f"{_avg_r(trades):+.2f}R"),
        ("רווח נטו", f"{gains - losses:+,.2f}"),
        ("זמן החזקה ממוצע", f"{sum(hold_days) / len(hold_days):.1f} ימים" if hold_days else "—"),
        ("רצף הפסדים ארוך ביותר", str(longest)),
    ]
    if len(trades) < MIN_TRADES * 2:
        card.findings.append(
            f"רק {len(trades)} עסקאות. ציון אמין דורש לפחות {MIN_TRADES * 2}, ולכן עדיין לא ניתן ציון."
        )
        return card
    if profit_factor < 1:
        card.findings.append(
            f"יחס רווח/הפסד {profit_factor:.2f}: המערכת מפסידה יותר ממה שהיא מרוויחה. "
            "אל תעבור לכסף אמיתי לפני שזה משתפר."
        )
    if profit_factor >= 1:
        card.strengths.append(f"על כל דולר הפסד המערכת הרוויחה {profit_factor:.2f} דולר.")
    if profit_factor < 1.5:
        card.findings.append(
            f"יחס רווח/הפסד {profit_factor:.2f}. יעד סביר הוא 1.5 ומעלה; הדרך לשם היא לחתוך מהר יותר "
            "עסקאות שלא זזות, או להחזיק יותר זמן את המנצחות."
        )
    card.verdict = f"יחס רווח/הפסד {profit_factor:.2f}, תוחלת {_avg_r(trades):+.2f}R לעסקה"
    card.status = "good" if profit_factor >= 1.5 else "watch" if profit_factor >= 1 else "bad"
    return card


def _offense(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "offense",
        "סוכן התקפה",
        "מרכז את תקציב הסיכון במה שהוכיח את עצמו: תבנית עם רקורד מוכח מקבלת 100% "
        "מתקרת הסיכון, תבנית חדשה 75% ותבנית מפסידה 50%. מסדר את ההזדמנויות כך שהחזקות "
        "תופסות ראשונות את המקומות המוגבלים ביום. לעולם לא חורג מרמת הסיכון שנבחרה.",
    )
    sized = _events(data, "offense", "sized")
    shares = Counter(e["detail"].split("|")[0] for e in sized)
    names = {f"{v:.2f}": k for k, v in CONVICTION.items()}
    labels = {"proven": "גודל מלא (מוכח)", "unproven": "75% (לא מוכח עדיין)", "losing": "50% (מפסיד)"}
    for share, count in sorted(shares.items(), reverse=True):
        card.kpis.append((labels.get(names.get(share, ""), share), str(count)))

    by_setup: dict[str, list[float]] = {}
    for trade in data.trades:
        by_setup.setdefault(trade.setup.value, []).append(trade.r_multiple())
    for setup, rs in sorted(by_setup.items()):
        edge = SetupEdge(len(rs), sum(rs) / len(rs))
        name = SETUP_NAMES.get(setup, setup)
        if edge.verdict == "proven":
            card.strengths.append(f"'{name}' הוכיחה את עצמה ({edge.expectancy:+.2f}R) ומקבלת גודל מלא.")
        elif edge.verdict == "losing":
            card.findings.append(
                f"'{name}' מפסידה ({edge.expectancy:+.2f}R על {edge.trades}) ומקבלת חצי גודל. "
                "אם זה נמשך, עדיף לחסום אותה לגמרי."
            )
        elif edge.trades < PROVEN_TRADES:
            card.findings.append(
                f"'{name}': {edge.trades} מתוך {PROVEN_TRADES} עסקאות שנדרשות כדי לקבל גודל מלא."
            )
    if sized:
        losing = any(SetupEdge(len(r), sum(r) / len(r)).verdict == "losing" for r in by_setup.values())
        card.status = "watch" if losing else "good"
    return card


def _loss_pct(pct: float) -> str:
    return f"-{pct:.2f}%" if pct > 0 else "0.00%"


def _book_loss(book: Book, price_of) -> float:
    """Loss if every open long were marked at price_of(trade)."""
    return sum(
        max(0.0, (t.entry_price - price_of(t)) * t.qty)
        for t in book.open_trades
        if t.side is Side.BUY
    )


def _breach_odds(book: Book, seed: int = 7) -> float | None:
    """Share of simulated futures that hit the drawdown halt within SIM_TRADES trades.

    Resamples this tenant's own R-multiples at the current per-trade risk; seeded,
    so the same history always reports the same number.
    """
    if len(book.r_history) < MIN_TRADES * 2:
        return None
    rng = random.Random(seed)
    risk = book.risk.risk_per_trade_pct / 100
    limit = 1 - book.risk.max_drawdown_pct / 100
    breaches = 0
    for _ in range(SIM_PATHS):
        equity = peak = 1.0
        for _ in range(SIM_TRADES):
            equity *= 1 + rng.choice(book.r_history) * risk
            peak = max(peak, equity)
            if equity <= peak * limit:
                breaches += 1
                break
    return breaches / SIM_PATHS


def _stress(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "stress",
        "סוכן מבחני קיצון",
        "תוקף את התיק לפני שהשוק תוקף: בודק מה יקרה בפער מחיר של 10% מעבר לכל הסטופים, "
        "בקריסה של 30%, ברצף הפסדים, ומריץ אלף תרחישי עתיד מתוך העסקאות שלך.",
    )
    statuses: list[str] = []
    for book in data.books:
        if book.equity <= 0:
            continue
        tag = f"{book.client_id}: " if len(data.books) > 1 else ""
        r = book.risk
        gap = _book_loss(book, lambda t: t.stop_price * (1 - GAP_THROUGH_STOP)) / book.equity * 100
        crash = _book_loss(book, lambda t: t.entry_price * (1 - CRASH)) / book.equity * 100
        streak = math.ceil(r.max_drawdown_pct / r.risk_per_trade_pct)
        odds = _breach_odds(book)
        floor_room = 100 - r.capital_floor_pct if r.capital_floor_pct else None

        card.kpis += [
            (f"{tag}פער 10% מעבר לסטופים", _loss_pct(gap)),
            (f"{tag}קריסה של 30%", _loss_pct(crash)),
            (f"{tag}הפסדים רצופים עד עצירה", str(streak)),
        ]
        if odds is not None:
            card.kpis.append((f"{tag}סיכוי לעצירה ב-{SIM_TRADES} עסקאות", f"{odds:.0%}"))

        status = "good"
        if book.open_trades and gap > r.max_daily_loss_pct:
            status = "watch"
            card.findings.append(
                f"{tag}פער מחיר של 10% מעבר לסטופים יגרום להפסד של {gap:.2f}%, יותר ממגבלת "
                f"ההפסד היומי ({r.max_daily_loss_pct:g}%). כדאי להקטין פוזיציות לפני דוחות וסופי שבוע."
            )
        if floor_room is not None and crash > floor_room:
            status = "bad"
            card.findings.append(
                f"{tag}בקריסה של 30% התיק יורד {crash:.2f}%, מתחת לרצפת הקרן ({r.capital_floor_pct:g}%). "
                "הרצפה היא יעד ולא הבטחה, וזה התרחיש שבו היא נשברת."
            )
        if odds is not None and odds > 0.2:
            status = "bad"
            card.findings.append(
                f"{tag}ב-{odds:.0%} מהתרחישים המערכת תגיע לעצירת מסחר תוך {SIM_TRADES} עסקאות. "
                "הסיכון לעסקה גבוה ביחס לביצועים בפועל: כדאי לרדת ברמת הסיכון."
            )
        elif odds is not None and odds > 0.05 and status == "good":
            status = "watch"
        if not book.open_trades and odds is None:
            status = "no_data"
        statuses.append(status)

    for rank in ("bad", "watch", "good"):
        if rank in statuses:
            card.status = rank
            break
    return card


def _index(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "index",
        "רובוט המדדים",
        "מחזיק קרנות מדד כהשקעה ארוכה מתקציב נפרד, בלי סטופ. כסף חדש נכנס בירידות או בשלישים, "
        "ובביטוח שוק דובי הוא מחזיק חצי במגמת ירידה ארוכה. הספרים שלו נפרדים מהרובוט הראשי.",
    )
    events = _events(data, "index")
    if not events:
        return card
    kinds = Counter(e["kind"] for e in events)
    card.kpis += [
        ("קניות", str(kinds.get("buy", 0))),
        ("מכירות", str(kinds.get("sell", 0))),
        ("לא בוצעו", str(kinds.get("unfilled", 0))),
        ("תקלות", str(kinds.get("failed", 0))),
    ]
    for event in events:
        if event["kind"] == "failed":
            card.findings.append(f"{event['symbol']}: {event['detail']}")
    card.status = "bad" if kinds.get("failed") else "watch" if kinds.get("unfilled") else "good"
    return card


def _replay(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "replay",
        "סוכן הבדיקה ההיסטורית",
        "מריץ את הרובוט עם ההגדרות של כל לקוח על 3 שנים של שוק אמיתי, ומראה אילו דרכי "
        "כניסה ואילו מניות מרוויחות ואילו מפסידות.",
    )
    # A losing setup is a question, not a verdict: on 2026-10-21 dropping tal_dahan's
    # losing pullbacks cut the replay from +16.5% to +10.8%, because the freed
    # slots went to weaker trend trades.
    losing_setups = False
    for client_id, result in data.replays:
        ret, bench = result["total_return_pct"], result["benchmark_return_pct"]
        card.kpis.append((f"{client_id}: הרובוט מול המדד", f"{ret:+.1f}% / {bench:+.1f}%"))
        for row in sorted(result["by_setup"], key=lambda r: r["pnl"]):
            if row["trades"] >= REPLAY_MIN_TRADES and row["pnl"] < 0:
                losing_setups = True
                card.findings.append(
                    f"{client_id}: כניסה ב{SETUP_NAMES.get(row['name'], row['name'])} הפסידה "
                    f"${-row['pnl']:,.0f} ב-{row['trades']} עסקאות. לפני שמורידים אותה, להריץ בדיקה "
                    "היסטורית בלעדיה: מקום פנוי בתיק מתמלא בעסקה אחרת, ולא תמיד טובה יותר."
                )
        worst = sorted((r for r in result["by_symbol"] if r["trades"] >= 3 and r["pnl"] < 0),
                       key=lambda r: r["pnl"])
        for row in worst[:2]:
            card.findings.append(
                f"{client_id}: {row['name']} הפסידה ${-row['pnl']:,.0f} ב-{row['trades']} עסקאות "
                f"({row['win_rate']:.0%} הצלחה). כדאי לשקול להוציא אותה."
            )
        if ret < bench / 2:
            card.findings.append(
                f"{client_id}: הרובוט הרוויח {ret:+.1f}% מול {bench:+.1f}% למדד. רוב הכסף לא עובד; "
                "רובוט המדדים יכול להחזיק את הכסף הפנוי."
            )
    if data.replays:
        card.status = "watch" if losing_setups or card.findings else "good"
    return card


def _hunter(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "hunter",
        "צייד המובילות",
        "פעם בשבוע מדרג את כל המניות במאגר המאושר לפי העלייה ב-3 וב-6 חודשים ומגמה "
        "בריאה, ומשאיר לרובוט רק את המובילות.",
    )
    picks = _events(data, "hunter", "leaders")
    if not picks:
        return card
    latest = picks[-1]["detail"].split()
    card.kpis.append(("המובילות עכשיו", " ".join(p.split(":")[0] for p in latest) or "—"))
    card.strengths.append("הרובוט מתרכז במניות החזקות של השוק במקום לפזר על כל המאגר.")
    card.findings.append(
        "בבדיקה ההיסטורית מניות 'מובילות' לבדן לא ניצחו את המדד. כדאי לבדוק בבדיקה ההיסטורית "
        "מספר מובילות שונה (למשל 5 או 12) לפני שמשנים."
    )
    card.status = "watch"
    return card


def _talent_scout(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "talent_scout",
        "הסקאוט (סוכן אנליסט)",
        "כל ערב עובר על המובילות, על המניות הכי נסחרות ועל אלה שזינקו, ונותן ציון A/B/C "
        "לפי כושר, חוזק מול המדד, חדשות, אנליסטים, צמיחה וסיכון.",
    )
    graded = [o for o in data.scout_outcomes if o.call.label in ("A", "B", "C")]
    top = [o for o in graded if o.call.label == "A"]
    long_shots = [o for o in data.scout_outcomes if o.call.label == "S"]
    card.kpis.append(("המלצות בנות שבוע ומעלה", str(len(graded) + len(long_shots))))
    if top:
        _calls_kpis(card, "ציון A", top)
    if graded:
        _calls_kpis(card, "כל ההמלצות", graded)
    if long_shots:
        _calls_kpis(card, "🚀 הימורים", long_shots)
        if _avg([o.ret for o in long_shots]) < -10:
            card.findings.append("ההימורים (מניות זולות) ירדו ביותר מ-10% בממוצע. כדאי להחמיר את תנאי הכניסה שלהם.")
    _judge_calls(card, top or graded, "מניות ציון A" if top else "המלצות הסקאוט")
    return card


def _money_flow(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "money_flow",
        "סוכן זרימת הכסף",
        "מזהה קנייה חריגה: נפח של פי 2 ומעלה וסגירה קרוב לשיא היום, וזרימת כסף ב-20 יום. "
        "נותן משקל רק אם במניה עצמה זה עבד בעבר.",
    )
    flows = [o for o in data.scout_outcomes if o.call.label == "F"]
    card.kpis.append(("קניות חריגות שנבדקו", str(len(flows))))
    if flows:
        _calls_kpis(card, "🐋 קנייה חריגה", flows)
    _judge_calls(card, flows, "מניות עם קנייה חריגה")
    card.findings.append(
        "במחקר של 10 שנים על המניות הגדולות, קנייה חריגה לבדה לא הקדימה את המדד. "
        "הסוכן מקבל משקל רק דרך ההיסטוריה של כל מניה, וזה צריך להישאר כך עד שהרקורד שלו יוכיח אחרת."
    )
    return card


def _researcher(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "researcher",
        "חוקר השוק",
        "כל ערב קורא ברשת את מה שהזיז את השוק: גיאופוליטיקה, מלחמות, ריבית, אנרגיה ודוחות, "
        "בודק את זה מול תנועות המדדים והסקטורים, ובוחר נושאים עם המניות שירוויחו מהם.",
    )
    results = data.research_outcomes
    card.kpis.append(("מניות שהוא הציע, בנות שבוע ומעלה", str(len(results))))
    if results:
        _calls_kpis(card, "הנושאים שלו", results)
    _judge_calls(card, results, "מניות הנושאים")
    return card


def _analyst(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "analyst",
        "מנתח הגרפים והניתוח המלא",
        "קורא את הגרף (מגמה, תמיכה, פריצה), בודק איך אותה קריאה עבדה במניה בעבר, מריץ את "
        "מעבדת האסטרטגיות על 5 שנים ומחבר הכול להמלצה: כן, בזהירות או לא עכשיו.",
    )
    by_level = {level: [o for o in data.analysis_outcomes if o.call.label == level]
                for level in ("go", "careful", "no")}
    names = {"go": "✅ כן", "careful": "🟡 בזהירות", "no": "⛔ לא עכשיו"}
    card.kpis.append(("ניתוחים בני שבוע ומעלה", str(len(data.analysis_outcomes))))
    for level, results in by_level.items():
        if results:
            _calls_kpis(card, names[level], results)
    go, no = by_level["go"] + by_level["careful"], by_level["no"]
    if len(go) >= ANALYST_MIN_CALLS and len(no) >= ANALYST_MIN_CALLS:
        gap = _avg([o.ret for o in go]) - _avg([o.ret for o in no])
        card.verdict = f"המלצות 'כן' {'הקדימו' if gap >= 0 else 'פיגרו אחרי'} את 'לא עכשיו' ב-{abs(gap):.1f}%"
        if gap >= 1:
            card.status = "good"
            card.strengths.append(f"מה שהוא המליץ לקנות עלה {gap:.1f}% יותר ממה שהוא אמר לחכות איתו.")
        else:
            card.status = "watch" if gap > -1 else "bad"
            card.findings.append(
                "ההמלצות לא מבדילות בין מניות טובות לרעות: 'כן' לא עשה טוב יותר מ'לא עכשיו'. "
                "כדאי לשנות את משקלי הציון."
            )
    else:
        _judge_calls(card, go, "המלצות 'כן'")
    return card


def _alerts(data: ReportInput) -> AgentCard:
    card = AgentCard(
        "alerts",
        "סוכן ההתראות והטלפון",
        "שולח לטלפון הודעה על כל קנייה ומכירה, התראות על תקלות, ושאלת בוקר לפני הפתיחה על "
        "מניות ברווח יפה. בודק כל סבב שיש לכל פוזיציה הגנה אצל הברוקר.",
    )
    if not data.ops:
        return card
    connected = all(o.phone_connected for o in data.ops)
    card.kpis += [
        ("טלגרם מחובר", "כן" if connected else "לא"),
        ("התראות פתוחות", str(sum(o.open_alerts for o in data.ops))),
        ("שאלות בוקר / נענו", f"{sum(o.questions_asked for o in data.ops)} / "
                              f"{sum(o.questions_answered for o in data.ops)}"),
    ]
    if not connected:
        card.status = "bad"
        card.verdict = "הטלפון לא מחובר: ההתראות וההודעות לא מגיעות אליך"
        card.findings.append(
            "חבר טלגרם (שני משתנים ב-Render). בלי זה, תקלה כמו מניה בלי הגנה תתגלה רק כשתיכנס לאתר."
        )
    else:
        card.status = "watch" if sum(o.open_alerts for o in data.ops) else "good"
        card.strengths.append("כל קנייה, מכירה ותקלה מגיעות לטלפון בזמן אמת.")
    return card


def _head(cards: Sequence[AgentCard]) -> AgentCard:
    card = AgentCard(
        "head",
        "ראש הסוכנים",
        "עובר על הדוחות של כל הסוכנים, מסמן מי עובד טוב ומי צריך טיפול, "
        "ומרכז את הפעולות החשובות ביותר לשיפור.",
    )
    statuses = Counter(c.status for c in cards)
    card.kpis += [
        ("עובדים טוב", str(statuses.get("good", 0))),
        ("דורשים מעקב", str(statuses.get("watch", 0))),
        ("דורשים טיפול", str(statuses.get("bad", 0))),
        ("אין מספיק נתונים", str(statuses.get("no_data", 0))),
    ]
    for rank in ("bad", "watch"):
        for agent in cards:
            if agent.status == rank:
                card.findings.extend(f"{agent.name}: {f}" for f in agent.findings[:1])
    card.findings = card.findings[:5]
    if statuses.get("bad"):
        card.status = "bad"
    elif statuses.get("watch"):
        card.status = "watch"
    elif statuses.get("good"):
        card.status = "good"
    return card
