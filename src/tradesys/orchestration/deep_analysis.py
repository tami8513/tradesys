"""The full analysis behind a "should I send the robot?" button.

Every input becomes one line with its points, so the client sees exactly why
the answer is what it is: the chart now, how that chart reading worked on this
stock before, how the robot's own method did on its last five years (on a year
it was not tuned on), the week's news, the analysts, growth, and an earnings
report days away. It is a transparent checklist, not a forecast.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradesys.agents.chart_analyst import ChartReading
from tradesys.agents.money_flow import FlowReading, describe, flow_points
from tradesys.agents.valuation import judge, read_valuation
from tradesys.agents.strategy_lab import LabReport
from tradesys.agents.talent_scout import red_flags, sentiment, story_text

GO, CAREFUL, NO = "go", "careful", "no"
HEADLINES = {
    GO: "✅ כן: שווה לשלוח את הרובוט",
    CAREFUL: "🟡 אפשר, בזהירות ובסכום קטן",
    NO: "⛔ לא עכשיו",
}


@dataclass(slots=True)
class Verdict:
    level: str
    score: int
    reasons: list[tuple[int, str]] = field(default_factory=list)   # (points, why)
    hold_better: bool = False

    @property
    def headline(self) -> str:
        return HEADLINES[self.level]


def verdict(
    reading: ChartReading | None,
    lab: LabReport | None,
    news: list[dict],
    analysts: dict | None = None,
    growth: dict | None = None,
    earnings_in_days: int | None = None,
    flow: FlowReading | None = None,
) -> Verdict:
    reasons: list[tuple[int, str]] = []

    if reading is not None:
        points = {"enter": 2, "maybe": 1, "wait": 0, "avoid": -2}[reading.action]
        reasons.append((points, f"הגרף עכשיו: {reading.headline}"))
        evidence = reading.evidence
        if evidence is not None and evidence.cases >= 5:
            edge = evidence.win_rate - evidence.base_win_rate
            if edge >= 5:
                reasons.append((1, f"מצב דומה בעבר: עלייה אחרי חודש ב-{evidence.win_rate:.0f}% מהמקרים, "
                                   f"יותר מיום רגיל ({evidence.base_win_rate:.0f}%)"))
            elif edge <= -5:
                reasons.append((-1, f"מצב דומה בעבר עבד פחות טוב מיום רגיל "
                                    f"({evidence.win_rate:.0f}% מול {evidence.base_win_rate:.0f}%)"))

    hold_better = False
    if lab is not None:
        best = lab.best
        if best.train.total <= 0:
            reasons.append((-2, f"שיטת הרובוט הפסידה על המניה הזו ב-4 השנים הראשונות ({best.train.total:+.0f}%)"))
        elif lab.holds_up:
            reasons.append((1, f"שיטת הרובוט ({best.label}) הרוויחה {best.train.total:+.0f}% ב-4 שנים, "
                               f"וגם {best.test.total:+.0f}% בשנה האחרונה שלא שימשה לבחירה"))
        else:
            reasons.append((-1, f"שיטת הרובוט הרוויחה ב-4 שנים ({best.train.total:+.0f}%) אבל הפסידה "
                                f"בשנה האחרונה ({best.test.total:+.0f}%): ייתכן שהיא כבר לא מתאימה"))
        if lab.beats_holding:
            reasons.append((1, "שיטת הרובוט ניצחה החזקה פשוטה של המניה, גם בשנה האחרונה"))
        hold_better = not lab.beats_holding and lab.hold_test.total > 0

    if news:
        tone = sum(sentiment(story_text(n)) for n in news)
        # Headlines only: a summary's "product offering" is not a share sale.
        flags = red_flags([{"headline": n.get("headline", "")} for n in news])
        if flags:
            reasons.append((-2, f"דגל אדום בחדשות השבוע: {', '.join(flags)}"))
        if tone > 0:
            reasons.append((1, f"{len(news)} כתבות השבוע, הטון חיובי"))
        elif tone < 0:
            reasons.append((-1, f"{len(news)} כתבות השבוע, הטון שלילי"))
    else:
        reasons.append((0, "כמעט אין עליה חדשות השבוע"))

    if analysts:
        total = sum(analysts.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
        bullish = analysts.get("strongBuy", 0) + analysts.get("buy", 0)
        bearish = analysts.get("sell", 0) + analysts.get("strongSell", 0)
        if total and bullish / total >= 0.6:
            reasons.append((1, f"{bullish} מתוך {total} אנליסטים ממליצים לקנות"))
        elif bearish > bullish:
            reasons.append((-1, "יותר אנליסטים ממליצים למכור מאשר לקנות"))

    value = read_valuation(growth, (growth or {}).get("pe_history"))
    if value is not None:
        good, bad, points = judge(value)
        reasons.append((points, f"שווי: {value.summary()}"))
        reasons += [(0, line) for line in good + bad]

    if growth:
        revenue, eps = growth.get("revenueGrowthTTMYoy"), growth.get("epsGrowthTTMYoy")
        if isinstance(revenue, (int, float)) and revenue > 15:
            reasons.append((1, f"ההכנסות צומחות ב-{revenue:.0f}% בשנה"))
        if isinstance(eps, (int, float)) and eps < 0:
            reasons.append((-1, "הרווח למניה ירד בשנה האחרונה"))

    if flow is not None:
        scored = flow_points(flow)
        if scored is not None:
            reasons.append(scored)
        elif flow.unusual_selling:
            reasons.append((0, describe(flow)[0]))
        reasons.append((0, describe(flow)[-1]))

    if earnings_in_days is not None and earnings_in_days <= 7:
        reasons.append((-1, f"דוח רבעוני בעוד {earnings_in_days} ימים: המחיר עלול לקפוץ לכל כיוון"))

    score = sum(points for points, _ in reasons)
    level = GO if score >= 4 else CAREFUL if score >= 2 else NO
    return Verdict(level, score, reasons, hold_better)


def gather(bars, feeds, symbol: str) -> dict:
    """Everything the full analysis reads, from ten years of daily bars and the feeds.

    Shared by the portal's analyze button and the Telegram market expert, so
    both see exactly the same analysis.
    """
    from tradesys.agents.chart_analyst import analyze
    from tradesys.agents.money_flow import read_flow
    from tradesys.agents.strategy_lab import LOOKBACK, YEARS, run_lab

    return {
        "reading": analyze(symbol, bars),
        "lab": run_lab(bars[-(LOOKBACK + YEARS * 252 + 20):]),
        # Stories about this stock, not round-ups that name it among many.
        "news": [n for n in feeds.news([symbol]) if symbol in n["symbols"] and len(n["symbols"]) <= 3],
        "analysts": feeds.analysts(symbol),
        "growth": feeds.growth(symbol),
        "earnings": feeds.earnings_in_days(symbol),
        "flow": read_flow(bars),
    }
