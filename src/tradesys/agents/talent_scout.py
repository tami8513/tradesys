"""The talent scout: roams the market and the news, and brings back a shortlist.

Like a football scout it does not buy anyone. It watches a wide pool (the
approved universe, today's most-traded names and today's biggest gainers), drops
what is not worth a look (thin, warrants, leveraged funds, falling), and writes a
short report on the most promising: current form, strength against the market,
what the news says, what analysts say, the company's growth, and the injury risk
(volatility, an earnings report days away).

Stocks between $1 and $5 get a separate, stricter lane, the long shots: they
qualify only on excellent news while short sellers hold a large position, the
setup in which buying back those shorts can push the price up fast.

Every grade is a transparent blend of measured facts, scored against the rest of
the pool. It is a filter that saves the client time, not a forecast: the same
momentum leaders lagged the index in this system's own three-year replay.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from tradesys.core.fileio import write_json_atomic
from tradesys.data.indicators import atr, closes, sma
from tradesys.data.market_data import MarketDataProvider
from tradesys.data.sectors import SECTORS, sector_label

MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 20_000_000.0
DEEP_LOOK = 40           # names that get news, analyst and growth checks
SHORTLIST = 10
LONG_SHOT_MIN_PRICE = 1.0
LONG_SHOT_MIN_DOLLAR_VOLUME = 10_000_000.0
LONG_SHOT_MIN_DAYS_TO_COVER = 3.0    # trading days the shorts need to buy back
LONG_SHOT_MIN_STORIES = 2
LONG_SHOT_MIN_TONE = 3
LONG_SHOTS = 5
WEIGHTS = {"form": 30, "relative": 20, "news": 15, "analysts": 20, "growth": 15}
_TICKER = re.compile(r"^[A-Z]{1,5}$")
# On Nasdaq a fifth letter W, U, R or Q marks a warrant, unit, right or bankruptcy.
_SPECIAL_FIFTH = re.compile(r"^[A-Z]{4}[WURQ]$")
_EXCLUDED_NAMES = (
    "2X", "3X", "-1X", "INVERSE", "ULTRA", " BEAR", " BULL", "LEVERAGED",
    "WARRANT", " UNIT", " RIGHT",
)

POSITIVE = (
    "beat", "beats", "record", "surge", "soar", "jump", "upgrade", "raises", "raised",
    "strong", "growth", "partnership", "contract", "approval", "approved", "buyback",
    "outperform", "rally", "expands", "wins", "launch",
)
NEGATIVE = (
    "miss", "misses", "cut", "cuts", "downgrade", "lawsuit", "probe", "investigation",
    "fraud", "recall", "bankruptcy", "offering", "dilution", "plunge", "slump", "falls",
    "warning", "layoffs", "delist", "short seller", "sells", "sold", "decline",
)
RED_FLAGS = {
    "lawsuit": "תביעה", "probe": "חקירה", "investigation": "חקירה", "fraud": "הונאה",
    "bankruptcy": "פשיטת רגל", "offering": "הנפקת מניות", "dilution": "דילול",
    "delist": "מחיקה מהמסחר", "short seller": "דוח שורט", "recall": "ריקול",
}
# Points off the blended score. A red flag or an overstretched chart is exactly
# what a pure ranking misses: the strongest 3-month runner is often both.
PENALTY = 10.0
STRETCHED_PCT = 25.0


@dataclass(slots=True)
class ScoutReport:
    symbol: str
    name: str
    sector: str
    price: float
    grade: str
    score: float
    parts: dict[str, float] = field(default_factory=dict)      # component -> 0..100
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)


def _words(words: Sequence[str]) -> re.Pattern[str]:
    # Whole words only: "cut" must not fire on "execute".
    return re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b")


_POSITIVE, _NEGATIVE, _RED_FLAGS = _words(POSITIVE), _words(NEGATIVE), _words(RED_FLAGS)


def _sentiment(text: str) -> int:
    lowered = text.lower()
    return len(_POSITIVE.findall(lowered)) - len(_NEGATIVE.findall(lowered))


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    """Each name's rank in the pool, 0 (worst) to 100 (best); ties share a rank."""
    if len(values) == 1:
        return {symbol: 50.0 for symbol in values}
    span = len(values) - 1
    pool = list(values.values())
    return {
        symbol: (sum(v < value for v in pool) + (pool.count(value) - 1) / 2) / span * 100
        for symbol, value in values.items()
    }


def _grade(score: float) -> str:
    return "A" if score >= 75 else "B" if score >= 60 else "C"


def _text(item: dict) -> str:
    return (item.get("headline", "") + " " + item.get("summary", "")).lower()


def _red_flags(items: list[dict]) -> list[str]:
    found = sorted(set(_RED_FLAGS.findall(" ".join(_text(n) for n in items))))
    return list(dict.fromkeys(RED_FLAGS[flag] for flag in found))


class TalentScout:
    def __init__(
        self,
        market_data: MarketDataProvider,
        *,
        names: Callable[[str], str | None],
        movers: Callable[[], list[str]] = list,
        news: Callable[[list[str]], list[dict]] = lambda symbols: [],
        analysts: Callable[[str], dict | None] = lambda symbol: None,
        growth: Callable[[str], dict | None] = lambda symbol: None,
        earnings_in_days: Callable[[str], int | None] = lambda symbol: None,
        short_interest: Callable[[list[str]], dict[str, dict]] = lambda symbols: {},
    ) -> None:
        self._market = market_data
        self._names = names
        self._movers = movers
        self._news = news
        self._analysts = analysts
        self._growth = growth
        self._earnings_in_days = earnings_in_days
        self._short_interest = short_interest

    # ------------------------------------------------------------- the pool

    def _eligible(self, symbol: str) -> bool:
        name = self._names(symbol)
        return bool(
            name
            and _TICKER.match(symbol)
            and not _SPECIAL_FIFTH.match(symbol)
            and not any(w in name.upper() for w in _EXCLUDED_NAMES)
        )

    def _form(self, symbol: str) -> dict | None:
        """Technical form and the lane the name qualifies for, or None for neither."""
        try:
            bars = self._market.history(symbol, timeframe="1Day", limit=220)
        except Exception:  # an unreadable name is skipped, not fatal
            return None
        if len(bars) < 63:
            return None
        values = closes(bars)
        last, sma50, sma200 = values[-1], sma(values, 50), sma(values, 200)
        dollar_volume = sum(b.close * b.volume for b in bars[-20:]) / 20
        if last >= MIN_PRICE:
            if dollar_volume < MIN_DOLLAR_VOLUME or sma200 is None or not last > sma50 > sma200:
                return None
            lane = "main"
        elif last >= LONG_SHOT_MIN_PRICE and dollar_volume >= LONG_SHOT_MIN_DOLLAR_VOLUME:
            lane = "long_shot"
        else:
            return None
        return {
            "lane": lane,
            "price": last,
            "r3m": (last / values[-63] - 1) * 100,
            "r6m": (last / values[-126] - 1) * 100 if len(values) >= 126 else 0.0,
            "atr_pct": (atr(bars) or 0.0) / last * 100,
            "extended": (last / sma50 - 1) * 100,
        }

    def _stories(self, symbols: list[str]) -> dict[str, list[dict]]:
        stories: dict[str, list[dict]] = {s: [] for s in symbols}
        for item in self._news(symbols) if symbols else []:
            for symbol in item.get("symbols", []):
                if symbol in stories:
                    stories[symbol].append(item)
        return stories

    # ----------------------------------------------------------- the report

    def shortlist(self, universe: Sequence[str], benchmark_6m: float) -> list[ScoutReport]:
        """The best regular names first, then the long shots (grade S)."""
        pool = sorted({s for s in [*universe, *self._movers()] if self._eligible(s)})
        forms = {s: f for s in pool if (f := self._form(s)) is not None}
        main = {s: f for s, f in forms.items() if f["lane"] == "main"}
        cheap = {s: f for s, f in forms.items() if f["lane"] == "long_shot"}
        return self._regulars(main, benchmark_6m) + self._long_shots(cheap)

    def _regulars(self, forms: dict[str, dict], benchmark_6m: float) -> list[ScoutReport]:
        if not forms:
            return []
        form_rank = _percentiles({s: (f["r3m"] + f["r6m"]) / 2 for s, f in forms.items()})
        deep = sorted(forms, key=form_rank.get, reverse=True)[:DEEP_LOOK]
        relative = _percentiles({s: forms[s]["r6m"] - benchmark_6m for s in deep})

        stories = self._stories(deep)
        news_raw = {s: sum(_sentiment(_text(n)) for n in items) for s, items in stories.items()}
        news_rank = _percentiles(news_raw)

        reports = []
        for symbol in deep:
            form = forms[symbol]
            parts = {"form": form_rank[symbol], "relative": relative[symbol], "news": news_rank[symbol]}
            report = ScoutReport(
                symbol=symbol,
                name=self._names(symbol) or symbol,
                sector=sector_label(symbol) if symbol in SECTORS else "",
                price=form["price"],
                grade="C",
                score=0.0,
                headlines=[n.get("headline", "") for n in stories[symbol][:3]],
            )
            penalty = self._describe_form(report, form, benchmark_6m)
            penalty += self._describe_news(report, stories[symbol], news_raw[symbol])

            recommendation = self._analysts(symbol)
            if recommendation:
                parts["analysts"] = self._describe_analysts(report, recommendation)
            metrics = self._growth(symbol)
            if metrics:
                parts["growth"] = self._describe_growth(report, metrics)
            self._describe_risk(report, form)

            weight = sum(WEIGHTS[k] for k in parts)
            report.score = max(0.0, sum(parts[k] * WEIGHTS[k] for k in parts) / weight - penalty)
            report.grade = _grade(report.score)
            report.parts = parts
            reports.append(report)
        return sorted(reports, key=lambda r: r.score, reverse=True)[:SHORTLIST]

    def _long_shots(self, forms: dict[str, dict]) -> list[ScoutReport]:
        """Cheap names with excellent news while short sellers are heavily exposed."""
        shorts = self._short_interest(sorted(forms)) if forms else {}
        exposed = [
            s for s in sorted(forms)
            if (shorts.get(s) or {}).get("days_to_cover", 0.0) >= LONG_SHOT_MIN_DAYS_TO_COVER
        ]
        stories = self._stories(exposed)
        reports = []
        for symbol in exposed:
            items = stories[symbol]
            tone = sum(_sentiment(_text(n)) for n in items)
            if len(items) < LONG_SHOT_MIN_STORIES or tone < LONG_SHOT_MIN_TONE or _red_flags(items):
                continue
            form, short = forms[symbol], shorts[symbol]
            report = ScoutReport(
                symbol=symbol,
                name=self._names(symbol) or symbol,
                sector=sector_label(symbol) if symbol in SECTORS else "",
                price=form["price"],
                grade="S",
                score=float(tone),
                parts={"days_to_cover": short["days_to_cover"]},
                headlines=[n.get("headline", "") for n in items[:3]],
            )
            report.strengths.append(f"חדשות מעולות: {len(items)} כתבות השבוע, הטון חיובי מאוד")
            report.strengths.append(
                f"הרבה משקיעים מהמרים על ירידה (שורט): כדי לקנות בחזרה הם צריכים "
                f"{short['days_to_cover']:.1f} ימי מסחר. חדשות טובות עלולות לאלץ אותם "
                "לקנות מהר ולהאיץ את העלייה"
            )
            change = short.get("change_pct")
            if isinstance(change, (int, float)) and abs(change) >= 10:
                direction = "גדל" if change > 0 else "קטן"
                report.strengths.append(f"השורט {direction} ב-{abs(change):.0f}% בספירה האחרונה")
            report.weaknesses.append(
                "מניה זולה (מתחת ל-5 דולר): קפיצות חדות לשני הכיוונים. הימור, לא השקעה. "
                "רק סכום קטן"
            )
            self._describe_risk(report, form)
            reports.append(report)
        reports.sort(key=lambda r: (r.score, r.parts["days_to_cover"]), reverse=True)
        return reports[:LONG_SHOTS]

    @staticmethod
    def _describe_form(report: ScoutReport, form: dict, benchmark_6m: float) -> float:
        report.strengths.append(
            f"בכושר: {form['r3m']:+.0f}% בשלושה חודשים ו-{form['r6m']:+.0f}% בחצי שנה, "
            "מעל הממוצעים של 50 ו-200 יום"
        )
        ahead = form["r6m"] - benchmark_6m
        if ahead > 0:
            report.strengths.append(f"מנצחת את המדד בחצי השנה האחרונה ב-{ahead:.0f}%")
        else:
            report.weaknesses.append(f"מפגרת אחרי המדד בחצי השנה האחרונה ב-{-ahead:.0f}%")
        if form["extended"] > 15:
            report.weaknesses.append(
                f"מתוחה: {form['extended']:.0f}% מעל ממוצע 50 הימים, ייתכן תיקון לפני המשך"
            )
        return PENALTY if form["extended"] > STRETCHED_PCT else 0.0

    @staticmethod
    def _describe_news(report: ScoutReport, items: list[dict], tone: int) -> float:
        if not items:
            report.weaknesses.append("כמעט אין עליה חדשות השבוע, השוק לא מסתכל עליה")
            return 0.0
        flags = _red_flags(items)
        if flags:
            report.weaknesses.append(f"דגל אדום בחדשות: {', '.join(flags)}")
        if tone > 0:
            report.strengths.append(f"{len(items)} כתבות השבוע, הטון חיובי")
        elif tone < 0:
            report.weaknesses.append(f"{len(items)} כתבות השבוע, הטון שלילי")
        return PENALTY if flags else 0.0

    @staticmethod
    def _describe_analysts(report: ScoutReport, rec: dict) -> float:
        buy = rec.get("strongBuy", 0) * 2 + rec.get("buy", 0)
        sell = rec.get("strongSell", 0) * 2 + rec.get("sell", 0)
        total = sum(rec.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
        if not total:
            return 50.0
        bullish = rec.get("strongBuy", 0) + rec.get("buy", 0)
        share = bullish / total * 100
        if share >= 60:
            report.strengths.append(f"{bullish} מתוך {total} אנליסטים ממליצים לקנות")
        elif rec.get("sell", 0) + rec.get("strongSell", 0) > bullish:
            report.weaknesses.append("יותר אנליסטים ממליצים למכור מאשר לקנות")
        return max(0.0, min(100.0, 50 + (buy - sell) / total * 25))

    @staticmethod
    def _describe_growth(report: ScoutReport, metrics: dict) -> float:
        revenue = metrics.get("revenueGrowthTTMYoy")
        eps = metrics.get("epsGrowthTTMYoy")
        known = [g for g in (revenue, eps) if isinstance(g, (int, float))]
        if not known:
            return 50.0
        if isinstance(revenue, (int, float)) and revenue > 15:
            report.strengths.append(f"ההכנסות צומחות ב-{revenue:.0f}% בשנה")
        if isinstance(eps, (int, float)) and eps < 0:
            report.weaknesses.append("הרווח למניה ירד בשנה האחרונה")
        return max(0.0, min(100.0, 50 + sum(known) / len(known)))

    def _describe_risk(self, report: ScoutReport, form: dict) -> None:
        if form["atr_pct"] > 4:
            report.weaknesses.append(
                f"תנודתית: זזה בממוצע {form['atr_pct']:.1f}% ביום, צריך פוזיציה קטנה וסטופ רחב"
            )
        days = self._earnings_in_days(report.symbol)
        if days is not None and days <= 7:
            report.weaknesses.append(f"דוח רבעוני בעוד {days} ימים: המחיר עלול לקפוץ לכל כיוון")


# ---------------------------------------------------------------- storage

def report_path(data_dir: Path, day: date) -> Path:
    return Path(data_dir) / "_platform" / f"scout_{day.isoformat()}.json"


def save_reports(data_dir: Path, day: date, reports: list[ScoutReport]) -> None:
    path = report_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {
        "made_at": datetime.now(timezone.utc).isoformat(),
        "reports": [asdict(r) for r in reports],
    })


def load_reports(data_dir: Path, today: date) -> tuple[str, list[ScoutReport]] | None:
    """The latest report from the last few days, if any."""
    for back in range(5):
        path = report_path(data_dir, today - timedelta(days=back))
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw["made_at"], [ScoutReport(**r) for r in raw["reports"]]
    return None
