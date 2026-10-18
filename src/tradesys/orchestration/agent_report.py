"""Operator-only report card for each agent, built from what the journal recorded.

Every grade is computed from facts — closed trades, logged decisions, order
statuses — and an agent with too little history is reported as such rather than
graded. A confident score drawn from three trades would mislead exactly the
person using this report to decide what to change.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from tradesys.core.models import TradeRecord

MIN_TRADES = 5
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
    "manual": "ידני",
}


@dataclass(slots=True)
class AgentCard:
    key: str
    name: str
    role: str
    status: str = "no_data"            # good | watch | bad | no_data
    kpis: list[tuple[str, str]] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReportInput:
    trades: Sequence[TradeRecord]        # closed trades in the period
    events: Sequence[dict[str, Any]]     # agent_events rows in the period
    order_statuses: Sequence[str]
    active_directives: int
    memory_records: int


def build_report(data: ReportInput) -> list[AgentCard]:
    cards = [
        _scout(data),
        _risk(data),
        _execution(data),
        _exits(data),
        _learning(data),
        _efficiency(data),
    ]
    return [_head(cards), *cards]


def _events(data: ReportInput, agent: str, kind: str | None = None) -> list[dict[str, Any]]:
    return [
        e for e in data.events
        if e["agent"] == agent and (kind is None or e["kind"] == kind)
    ]


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
            card.findings.append(
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
        for part in e["detail"].split("; ")
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
    card.kpis.append(("פקודות שנשלחו", str(total)))
    if total:
        filled = statuses.get("filled", 0)
        rate = filled / total
        card.kpis += [
            ("בוצעו", f"{rate:.0%}"),
            ("נדחו", str(statuses.get("rejected", 0))),
            ("בוטלו", str(statuses.get("canceled", 0))),
        ]
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
    card.status = "good" if profit_factor >= 1.5 else "watch" if profit_factor >= 1 else "bad"
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
    card.findings = card.findings[:3]
    if statuses.get("bad"):
        card.status = "bad"
    elif statuses.get("watch"):
        card.status = "watch"
    elif statuses.get("good"):
        card.status = "good"
    return card
