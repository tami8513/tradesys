"""The "why" journal: every recorded agent decision, explained in plain Hebrew.

Agents log reasons in terse English with numbers ("reward:risk 1.40 below
minimum 2.00"), which is right for logs and wrong for a client deciding whether
to trust the robot. Translation happens here, at display time, so the stored
record stays exact. An unrecognised reason is shown as recorded rather than
guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from tradesys.orchestration.agent_report import EXIT_NAMES, REASON_SEP, SETUP_NAMES

_REASONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), text)
    for pattern, text in (
        (r"^tenant halted: (.*)", r"המסחר מושהה: \1"),
        (r"^drawdown ([\d.]+)% >= limit ([\d.]+)%.*", r"הירידה מהשיא (\1%) הגיעה למגבלה (\2%), והמסחר נעצר"),
        (r"^daily loss ([\d.]+)% >= limit ([\d.]+)%.*", r"ההפסד היומי (\1%) הגיע למגבלה (\2%), והמסחר נעצר להיום"),
        (r"^(\S+) is not on the client watchlist", r"\1 לא נמצאת ברשימת המעקב"),
        (r"^setup \S+ not enabled for (\S+)", r"התבנית הזו לא מאושרת עבור \1"),
        (r"^short selling is disabled.*", "מכירה בחסר כבויה בחשבון הזה"),
        (r"^price ([\d.]+) below min_price ([\d.]+)", r"המחיר (\1) נמוך מהמינימום המותר (\2)"),
        (r"^stop is missing.*|^long stop must.*|^short stop must.*", "הסטופ לא הוגדר נכון, ולכן העסקה נפסלה"),
        (r"^reward:risk ([\d.]+) below minimum ([\d.]+)", r"הרווח הצפוי קטן מדי ביחס לסיכון (\1 מול מינימום \2)"),
        (r"^(\d+) open positions at limit (\d+)", r"כבר יש \1 פוזיציות פתוחות, וזו המגבלה (\2)"),
        (r"^already holding (\S+);.*", r"כבר מחזיקים ב-\1, ולא מוסיפים לאותה פוזיציה"),
        (r"^(\d+) trades today at limit (\d+)", r"כבר נפתחו \1 עסקאות היום, וזו המגבלה (\2)"),
        (r"^at capital floor.*", "התיק ברצפת הקרן. אין כניסות חדשות עד שהכרית תתאושש"),
        (r"^no earnings coverage for (\S+).*", r"אין מידע על מועד הדוח הבא של \1, ולכן הכניסה נחסמה ליתר ביטחון"),
        (r"^(\S+) reports in (\d+)d.*", r"\1 מפרסמת דוח בעוד \2 ימים. סטופ לא מגן מפני קפיצת מחיר של דוח"),
        (r"^market regime unavailable.*", "אין נתונים על מצב השוק, ולכן התבנית נחסמה ליתר ביטחון"),
        (r"^\S+ blocked in a risk-off market.*", "השוק במגמת ירידה (מתחת לממוצע 200), ופריצות חסומות במצב כזה"),
        (r"^risk budget [\d.]+ cannot cover one share.*", "תקציב הסיכון לעסקה קטן מדי אפילו למניה אחת"),
        (r"^position sizes to zero shares", "גודל הפוזיציה יצא אפס אחרי כל המגבלות"),
        (r"^duplicate client_order_id.*", "הרעיון הזה כבר נשלח היום, ולא שולחים אותו פעמיים"),
        (r"^broker rejected: (.*)", r"הברוקר דחה את הפקודה: \1"),
        (
            r"^moves with (\S+) \(correlation ([\d.]+).*",
            r"המניה זזה כמעט בדיוק כמו \1 שכבר בתיק (מתאם \2). שתיהן יחד הן אותו הימור פעמיים",
        ),
        (
            r"^reduced to 0 shares by the ([\d.]+)% cap on sector (\S+)",
            r"הענף כבר מלא: מותר עד \1% מהתיק בענף אחד",
        ),
        (r"^reduced to 0 shares by the client's allocation", "הסכום שהלקוח הקצה לרובוט כבר מושקע במלואו"),
        (r"^reduced to (\d+) shares by .*", r"הכמות הוקטנה ל-\1 מניות בגלל מגבלת גודל"),
        (r"^sized on the ([\d.]+) the client allocated.*", r"הגודל חושב לפי הסכום שהלקוח הקצה לרובוט ($\1)"),
        (r"^autopilot: ([\d.]+)% risk per trade.*", r"טייס אוטומטי: \1% סיכון לעסקה"),
        (r"^risk budget at (\d+)% of the ceiling by conviction", r"סוכן ההתקפה הקצה \1% מתקרת הסיכון"),
        (
            r"^standing approval of \$([\d,]+) buys less than one share",
            r"הסכום שאישרת מראש ($\1) לא מספיק אפילו למניה אחת",
        ),
    )
)

_RATIONALES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), text)
    for pattern, text in (
        (
            r"closed ([\d.]+) above the 20-bar high ([\d.]+) on ([\d.]+)x volume",
            r"נסגרה ב-\1, מעל השיא של 20 הימים האחרונים (\2), במחזור פי \3 מהרגיל",
        ),
        (
            r"pullback to the 20SMA \(([\d.]+)\) while above the 50SMA, RSI (\d+)",
            r"ירדה לממוצע 20 הימים (\1) ועדיין מעל ממוצע 50 הימים, RSI \2",
        ),
        (r"RSI (\d+) with an up-close reversal bar", r"מכירת יתר (RSI \1) ונר היפוך שנסגר בעלייה"),
        (r"price above a rising 20/50 SMA stack, RSI (\d+)", r"המחיר מעל ממוצעי 20 ו-50 העולים, RSI \1"),
    )
)

LOCAL_TZ = ZoneInfo("Asia/Jerusalem")

_STATUS = {
    "filled": "בוצעה",
    "accepted": "התקבלה אצל הברוקר",
    "partially_filled": "בוצעה חלקית",
    "canceled": "בוטלה כי לא בוצעה בזמן",
    "rejected": "נדחתה",
    "not_sent": "לא נשלחה",
}


@dataclass(frozen=True, slots=True)
class WhyEntry:
    when: str
    symbol: str
    text: str
    tone: str            # positive | negative | neutral


def explain_reason(reason: str) -> str:
    for pattern, text in _REASONS:
        if pattern.match(reason):
            return pattern.sub(text, reason, count=1)
    return reason


def explain_rationale(rationale: str) -> str:
    for pattern, text in _RATIONALES:
        rationale = pattern.sub(text, rationale)
    return rationale


def _local_time(stamp: str) -> str:
    """Stored in UTC; the client reads Israel time."""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp[:16]
    if moment.tzinfo is not None:
        moment = moment.astimezone(LOCAL_TZ)
    return moment.strftime("%Y-%m-%d %H:%M")


def _setup(name: str) -> str:
    return SETUP_NAMES.get(name, name)


def _entry(event: dict[str, Any]) -> WhyEntry | None:
    agent, kind, symbol = event["agent"], event["kind"], event["symbol"]
    detail = event["detail"] or ""
    when = _local_time(event["created_at"] or "")

    if agent == "scout" and kind == "candidate":
        setup, _, rationale = detail.partition("|")
        text = f"סוכן הכניסה זיהה {_setup(setup)}"
        if rationale:
            text += f": {explain_rationale(rationale)}"
        return WhyEntry(when, symbol, text, "neutral")
    if agent == "offense" and kind == "sized":
        share = float(detail.partition("|")[0] or 1)
        return WhyEntry(when, symbol, f"סוכן ההתקפה הקצה {share:.0%} מתקרת הסיכון", "neutral")
    if agent == "risk" and kind == "rejected":
        reasons = [explain_reason(r) for r in detail.split(REASON_SEP) if r]
        return WhyEntry(when, symbol, "שומר הסיכון חסם: " + " · ".join(reasons), "negative")
    if agent == "risk" and kind == "approved":
        return WhyEntry(when, symbol, "שומר הסיכון אישר את העסקה", "positive")
    if agent == "risk" and kind == "halted":
        return WhyEntry(when, "", "המסחר נעצר בסבב הזה: " + explain_reason(detail), "negative")
    if agent == "execution":
        text = f"הפקודה {_STATUS.get(kind, kind)}"
        if detail:
            text += f" ({explain_reason(detail)})"
        return WhyEntry(when, symbol, text, "positive" if kind == "filled" else "neutral")
    if agent == "monitor" and kind == "exit":
        reason, _, r = detail.partition("|")
        r_value = float(r or 0)
        tone = "positive" if r_value > 0 else "negative"
        label = EXIT_NAMES.get(reason, reason)
        if reason == "stop_loss" and r_value > 0:
            label = "סטופ נגרר שנעל רווח"
        return WhyEntry(when, symbol, f"סוכן היציאה סגר את העסקה ב{label}, תוצאה {r_value:+.2f}R", tone)
    if agent == "monitor" and kind == "stop_raised":
        prices = re.findall(r"[\d.]+", detail.split("stop", 1)[-1])
        if len(prices) >= 2:
            return WhyEntry(
                when, symbol, f"הרווח מתקדם: הסטופ עלה מ-{prices[0]} ל-{prices[1]}", "positive"
            )
    if agent == "monitor" and kind == "held_past_target":
        return WhyEntry(
            when, symbol, "העסקה הגיעה ליעד והמגמה עדיין עולה, אז נשארים בה עם סטופ נגרר", "positive"
        )
    if agent == "monitor" and kind == "error":
        return WhyEntry(when, symbol, f"תקלה בניהול הפוזיציה: {detail}", "negative")
    if agent == "execution" and kind == "awaiting_approval":
        return WhyEntry(
            when, symbol, f"הרובוט מצא הזדמנות ומחכה לאישור שלך ({detail} מניות)", "neutral"
        )
    if agent == "client" and kind == "approved":
        return WhyEntry(when, symbol, f"אישרת את הקנייה: {detail}", "positive")
    if agent == "client" and kind == "preapproved":
        return WhyEntry(
            when, symbol, f"נקנה לפי האישור המראש שלך ({detail} מניות), בלי לשאול שוב", "positive"
        )
    if agent == "client" and kind == "declined":
        return WhyEntry(when, symbol, "דחית את ההצעה, והרובוט לא קנה", "neutral")
    if agent == "reflection" and kind == "lessons":
        return WhyEntry(when, symbol, f"סוכן הלמידה כתב {detail} לקחים חדשים", "neutral")
    return None


def timeline(events: Sequence[dict[str, Any]], symbol: str | None = None) -> list[WhyEntry]:
    """Newest first. `symbol` narrows the journal to one stock."""
    entries = [e for e in (_entry(event) for event in events) if e is not None]
    if symbol:
        entries = [e for e in entries if e.symbol == symbol]
    return list(reversed(entries))


def latest_by_agent(events: Sequence[dict[str, Any]]) -> dict[str, WhyEntry]:
    """Each agent's most recent explained decision, for the home screen's team board."""
    latest: dict[str, WhyEntry] = {}
    for event in reversed(events):
        if event["agent"] in latest:
            continue
        entry = _entry(event)
        if entry is not None:
            latest[event["agent"]] = entry
    return latest
