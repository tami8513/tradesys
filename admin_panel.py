"""פורטל ניהול — שליטה בצי הלקוחות, מחזור חיים, חיוב ולמידה גלובלית.

נגיש רק אחרי כניסת מנהל במסך הכניסה הראשי.

שלוש מגבלות מובנות בקובץ הזה, במכוון:

  1. הצבירה הגלובלית קוראת **נתונים מספריים וקטגוריים בלבד** בין לקוחות —
     לעולם לא את MemoryRecord.text. ניתוחי עסקאות ומשוב מכילים מידע מזהה של
     הלקוח, וצבירה שלהם הייתה מבטלת את הבידוד שעליו המערכת בנויה.
     סטטיסטיקה ניתנת להכללה; משפטים דולפים.

  2. הקפאת לקוח כותבת עצירה ליומן שלו, לא רק דגל בממשק. מנהל הסיכונים
     הדטרמיניסטי קורא את העצירה, ולכן לקוח מוקפא חסום מפתיחת פוזיציות מכל
     נקודת כניסה — ממשק, CLI או cron. יציאות נשארות אפשריות תמיד.

  3. כל מסך שמציג ביצועי סימולציה נושא את גילוי הנאות ואת התמונה המלאה,
     לא רק את העסקאות הרווחיות. ראה DISCLOSURE למטה.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tradesys.core.clock import trading_day
from tradesys.core.errors import TradesysError

import accounts as acct

# חל על כל מסך שמציג ביצועי סימולציה. תוצאות נייר הן היפותטיות: לא נשלחה פקודה,
# אין החלקה, אין מילוי חלקי, אין עלויות השאלה ואין מס. הצגה שלהן ללא המסגור הזה
# היא בדיוק בעיית "ביצועים היפותטיים" ו"בחירת דובדבנים" שרגולטורים אוכפים.
DISCLOSURE = (
    "ביצועי סימולציה / היפותטיים — הופקו על ידי מנוע מסחר בחשבון נייר. "
    "לא נשלחו פקודות אמיתיות ולא הועמד הון אמיתי בסיכון. המילויים מניחים ביצוע "
    "מיידי במחיר האחרון, ללא החלקה, ללא מילוי חלקי, ללא עמלות, עלויות השאלה או מס. "
    "לתוצאות היפותטיות יש מגבלות מובנות והן אינן מייצגות מסחר בפועל. "
    "ביצועי עבר או סימולציה אינם מעידים על תוצאות עתידיות."
)

PALETTE = {
    "panel": "#131a2a",
    "border": "#26304a",
    "text": "#eef2fa",
    "muted": "#8d97b0",
    "accent": "#4cc4f5",
    "gold": "#d4af6a",
    "positive": "#26d07c",
    "negative": "#f2545b",
    "warning": "#f5a524",
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def ltr(value: object) -> str:
    return f'<span dir="ltr">{esc(value)}</span>'


# --------------------------------------------------------------- מחזור חיים

@dataclass(frozen=True, slots=True)
class Lifecycle:
    active: bool = True
    plan: str = "standard"          # standard | trial | suspended
    reason: str = ""
    changed_at: str = ""


def lifecycle_path(session) -> Path:
    return session.tenant.data_dir / "lifecycle.json"


def read_lifecycle(session) -> Lifecycle:
    path = lifecycle_path(session)
    if not path.exists():
        return Lifecycle()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Lifecycle(
            active=bool(raw.get("active", True)),
            plan=str(raw.get("plan", "standard")),
            reason=str(raw.get("reason", "")),
            changed_at=str(raw.get("changed_at", "")),
        )
    except (ValueError, OSError):
        return Lifecycle()


def write_lifecycle(session, lifecycle: Lifecycle) -> None:
    lifecycle_path(session).write_text(
        json.dumps(
            {
                "active": lifecycle.active,
                "plan": lifecycle.plan,
                "reason": lifecycle.reason,
                "changed_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def freeze_tenant(session, reason: str) -> None:
    """משהה לקוח מסיבות חיוב או מחזור חיים.

    כותב את דגל מחזור החיים *וגם* נועל עצירה ביומן. העצירה היא מה שמחייב
    בפועל: RiskManager.preflight דוחה כל כניסה כל עוד היא פעילה, ולכן גם
    משימת cron או ה-CLI לא יסחרו בלקוח מוקפא. יציאות נשארות זמינות במכוון —
    הקפאת לקוח לעולם לא אמורה ללכוד אותו בתוך פוזיציה פתוחה.
    """
    write_lifecycle(session, Lifecycle(active=False, plan="suspended", reason=reason))
    session.journal.halt(f"חשבון מושהה: {reason}")


def reactivate_tenant(session, plan: str = "standard") -> None:
    write_lifecycle(session, Lifecycle(active=True, plan=plan, reason="הופעל מחדש"))
    session.journal.clear_halt()


def graceful_exit(session, reason: str) -> list:
    """סוגר כל פוזיציה פתוחה, רושם רווח/הפסד וכותב ניתוח לכל אחת.

    משתמש באותו מסלול שבו סטופ-לוס היה סוגר, כך שיציאה עקב סיום מנוי
    נרשמת ומנותחת בדיוק כמו כל סגירה אחרת.
    """
    closed = []
    for symbol in sorted({t.symbol for t in session.journal.open_trades()}):
        report = session.monitor.close_manually(symbol, reason)
        closed.extend(report.closed)
    return closed


# ----------------------------------------------------------------- סקירת צי

@st.cache_data(ttl=15, show_spinner=False)
def load_fleet(client_ids: tuple[str, ...], bust: int) -> pd.DataFrame:
    """קריאה חוצת-לקוחות. המקום היחיד במערכת שמורשה לעבור על כל הלקוחות.

    כל לקוח עדיין נפתח דרך ה-ClientSession שלו, כך שהבידוד נשמר; מה שמשתנה
    הוא שהנמען של התוצאה הוא המנהל, לא לקוח.
    """
    del bust
    from app import get_session

    rows = []
    for client_id in client_ids:
        try:
            session = get_session(client_id)
            account = session.broker.account()
            stats = session.journal.day_stats(trading_day(session.clock))
            day_start = session.journal.day_start_equity()
            halted, halt_reason = session.journal.halt_state()
            lifecycle = read_lifecycle(session)
            positions = session.broker.positions()
            closed = session.journal.closed_trades(limit=1000)

            drawdown = (
                (stats.peak_equity - account.equity) / stats.peak_equity * 100
                if stats.peak_equity > 0
                else 0.0
            )
            if not lifecycle.active:
                health = "מושהה"
            elif halted:
                health = "עצור"
            elif drawdown >= session.profile.risk.max_drawdown_pct * 0.75:
                health = "בסיכון"
            else:
                health = "תקין"

            rows.append(
                {
                    "client_id": client_id,
                    "name": session.profile.display_name,
                    "plan": lifecycle.plan,
                    "active": lifecycle.active,
                    "health": health,
                    "equity": account.equity,
                    "day_pnl": account.equity - day_start if day_start > 0 else 0.0,
                    "realized_today": stats.realized_pnl,
                    "lifetime_pnl": sum(t.realized_pnl or 0.0 for t in closed),
                    "positions": len(positions),
                    "drawdown_pct": drawdown,
                    "paper": session.profile.broker.paper,
                    "closed_count": len(closed),
                    "halt_reason": halt_reason,
                }
            )
        except (TradesysError, OSError) as exc:
            rows.append(
                {
                    "client_id": client_id,
                    "name": "—",
                    "plan": "?",
                    "active": False,
                    "health": f"שגיאה: {exc}",
                    "equity": 0.0,
                    "day_pnl": 0.0,
                    "realized_today": 0.0,
                    "lifetime_pnl": 0.0,
                    "positions": 0,
                    "drawdown_pct": 0.0,
                    "paper": True,
                    "closed_count": 0,
                    "halt_reason": "",
                }
            )
    return pd.DataFrame(rows)


# ----------------------------------------------------------- למידה גלובלית

@st.cache_data(ttl=60, show_spinner=False)
def load_hive(client_ids: tuple[str, ...], bust: int) -> dict:
    """צובר למידה חוצת-לקוחות בלי לאחד טקסט של אף לקוח.

    קורא בלבד: תבנית, תוצאה, סיבת יציאה, מכפיל R ומשך החזקה. לעולם לא
    MemoryRecord.text ולא נימוק — אלה נושאים תוכן מזהה, ו"תובנה גלובלית"
    שהורכבה מהם היא דליפת פרטיות בתחפושת.
    """
    del bust
    from app import get_session

    facts: list[dict] = []
    for client_id in client_ids:
        try:
            session = get_session(client_id)
        except (TradesysError, OSError):
            continue
        for trade in session.journal.closed_trades(limit=2000):
            facts.append(
                {
                    "setup": trade.setup.value,
                    "r": trade.r_multiple(),
                    "pnl": trade.realized_pnl or 0.0,
                    "win": (trade.realized_pnl or 0.0) > 0,
                    "held_days": (
                        (trade.closed_at - trade.opened_at).total_seconds() / 86400
                        if trade.closed_at
                        else 0.0
                    ),
                    # במכוון לא נאסף: סימבול, נימוק, client_id, טקסט זיכרון
                }
            )

    if not facts:
        return {
            "frame": pd.DataFrame(),
            "by_setup": pd.DataFrame(),
            "tenants": len(client_ids),
        }

    frame = pd.DataFrame(facts)
    by_setup = (
        frame.groupby("setup")
        .agg(
            trades=("r", "size"),
            win_rate=("win", "mean"),
            avg_r=("r", "mean"),
            total_pnl=("pnl", "sum"),
            avg_hold=("held_days", "mean"),
        )
        .reset_index()
    )
    by_setup["win_rate"] = by_setup["win_rate"] * 100
    return {"frame": frame, "by_setup": by_setup, "tenants": len(client_ids)}


# --------------------------------------------------------------- דוח סוכנים

STATUS_STYLE = {
    "good": ("עובד טוב", PALETTE["positive"]),
    "watch": ("דורש מעקב", PALETTE["warning"]),
    "bad": ("דורש טיפול", PALETTE["negative"]),
    "no_data": ("אין מספיק נתונים", PALETTE["muted"]),
}


@st.cache_data(ttl=60, show_spinner=False)
def load_agent_report(client_ids: tuple[str, ...], days: int, bust: int) -> list:
    """Report cards over the chosen tenants. Reads journals only; writes nothing."""
    del bust
    from datetime import timedelta

    from app import get_session
    from tradesys.orchestration.agent_report import Book, ReportInput, build_report

    since = datetime.now(timezone.utc) - timedelta(days=days)
    trades, events, statuses, books = [], [], [], []
    directives = memory = 0
    for client_id in client_ids:
        try:
            session = get_session(client_id)
        except (TradesysError, OSError):
            continue
        try:
            equity = session.broker.account().equity
        except Exception:  # a broker outage must not blank the whole report
            equity = 0.0   # the stress tests skip a book without equity
        journal = session.journal
        history = journal.closed_trades(limit=5000)
        trades += [t for t in history if t.closed_at and t.closed_at >= since]
        events += journal.agent_events(since)
        statuses += journal.order_statuses()
        directives += len(journal.active_directives())
        memory += session.memory.count()
        books.append(
            Book(
                client_id,
                equity,
                journal.open_trades(),
                session.profile.risk,
                [t.r_multiple() for t in history],
            )
        )
    return build_report(ReportInput(trades, events, statuses, directives, memory, books))


def render_agents(client_ids: tuple[str, ...], bust: int) -> None:
    st.markdown("##### דוח סוכנים")
    st.caption(
        "לשימוש פנימי בלבד, לא מוצג ללקוחות. כל ציון מחושב מהעסקאות ומההחלטות "
        "שנרשמו בפועל. סוכן בלי מספיק היסטוריה מסומן כך, ולא מקבל ציון מומצא."
    )
    scope_col, period_col = st.columns([2, 1])
    scope = scope_col.selectbox(
        "לקוח", ["כל הלקוחות", *client_ids], key="agents_scope"
    )
    days = period_col.selectbox(
        "תקופה", [7, 30, 90, 365], index=1, format_func=lambda d: f"{d} ימים",
        key="agents_days",
    )
    chosen = client_ids if scope == "כל הלקוחות" else (scope,)
    cards = load_agent_report(chosen, days, bust)

    for card in cards:
        label, color = STATUS_STYLE[card.status]
        kpis = "".join(
            f'<div style="display:flex;justify-content:space-between;gap:12px;'
            f'padding:3px 0;border-bottom:1px solid {PALETTE["border"]}">'
            f'<span style="color:{PALETTE["muted"]}">{html.escape(k)}</span>'
            f"<b>{html.escape(v)}</b></div>"
            for k, v in card.kpis
        )
        findings = "".join(
            f'<li style="margin:4px 0">{html.escape(f)}</li>' for f in card.findings
        )
        head = card.key == "head"
        st.markdown(
            f'<div class="ts-card" style="border-color:{color if head else PALETTE["border"]};'
            f'margin-bottom:12px">'
            f'<div style="display:flex;justify-content:space-between;align-items:center">'
            f'<div style="font-size:{18 if head else 16}px;font-weight:700">'
            f"{html.escape(card.name)}</div>"
            f'<span style="color:{color};border:1px solid {color};border-radius:999px;'
            f'padding:2px 10px;font-size:12px">{label}</span></div>'
            f'<div style="color:{PALETTE["muted"]};font-size:13px;margin:6px 0 10px">'
            f"{html.escape(card.role)}</div>"
            f"{kpis}"
            + (f'<ul style="margin:10px 0 0;padding-inline-start:18px">{findings}</ul>'
               if findings else "")
            + "</div>",
            unsafe_allow_html=True,
        )


# ------------------------------------------------------------- ייצוא שיווקי

def build_share_card(session, top_n: int = 3) -> tuple[str, dict]:
    """מרכיב סיכום לשיתוף שנושא את ההקשר המלא שלו.

    העסקאות הרווחיות מוצגות *בתוך* התמונה המלאה — סך עסקאות, אחוז הצלחה
    ורווח נטו — כי פוסט שמציג רק עסקאות ירוקות מתוך תיק מפסיד הוא בחירת
    דובדבנים, והדרך המהירה ביותר להפוך שיווק לבעיה רגולטורית.
    גילוי הנאות ותווית נייר/אמיתי אינם פרמטרים אופציונליים.
    """
    closed = session.journal.closed_trades(limit=1000)
    if not closed:
        return "", {}

    wins = sorted(
        [t for t in closed if (t.realized_pnl or 0.0) > 0],
        key=lambda t: t.realized_pnl or 0.0,
        reverse=True,
    )[:top_n]
    total = sum(t.realized_pnl or 0.0 for t in closed)
    win_count = sum(1 for t in closed if (t.realized_pnl or 0.0) > 0)
    context = {
        "trades": len(closed),
        "wins": win_count,
        "win_rate": win_count / len(closed) * 100 if closed else 0.0,
        "net_pnl": total,
        "mode": "חשבון נייר" if session.profile.broker.paper else "חשבון אמיתי",
    }

    lines = [
        f"סוכן מסחר אוטונומי — תוצאות {context['mode']}",
        "",
        f"התמונה המלאה: {context['trades']} עסקאות סגורות · "
        f"{context['win_rate']:.0f}% הצלחה · נטו {total:+,.2f}",
        "",
        "עסקאות נבחרות:",
    ]
    for trade in wins:
        lines.append(
            f"  {trade.symbol} · {trade.setup.value} · "
            f"{trade.entry_price:.2f} -> {trade.exit_price:.2f} · "
            f"{trade.realized_pnl:+,.2f} ({trade.r_multiple():+.2f}R)"
        )
    lines += [
        "",
        "כל כניסה מתומחרת על ידי מנוע סיכונים דטרמיניסטי; "
        "כל יציאה מנוהלת על ידי מנטר סטופ/יעד אוטומטי.",
        "",
        DISCLOSURE,
    ]
    return "\n".join(lines), context


# ---------------------------------------------------------------- לשוניות

def render_fleet(fleet: pd.DataFrame) -> None:
    st.markdown("##### סקירת הצי")
    if fleet.empty:
        st.info("לא הוגדרו לקוחות.")
        return

    active = fleet[fleet["active"]]
    columns = st.columns(5)
    columns[0].metric("לקוחות", len(fleet), delta=f"{len(active)} פעילים", delta_color="off")
    columns[1].metric("סך נכסים מנוהלים", f"${fleet['equity'].sum():,.0f}")
    columns[2].metric("רווח/הפסד יומי בצי", f"{fleet['day_pnl'].sum():+,.2f}")
    columns[3].metric("פוזיציות פתוחות", int(fleet["positions"].sum()))
    columns[4].metric("דורשים טיפול", int((fleet["health"] != "תקין").sum()))

    st.markdown("")
    display = fleet[
        [
            "client_id",
            "name",
            "plan",
            "health",
            "equity",
            "day_pnl",
            "realized_today",
            "lifetime_pnl",
            "positions",
            "drawdown_pct",
        ]
    ].copy()
    display.columns = [
        "מזהה",
        "שם",
        "מסלול",
        "מצב",
        "שווי תיק",
        "יומי",
        "ממומש היום",
        "ממומש מצטבר",
        "פוזיציות",
        "ירידה %",
    ]
    st.dataframe(
        display.style.format(
            {
                "שווי תיק": "${:,.2f}",
                "יומי": "{:+,.2f}",
                "ממומש היום": "{:+,.2f}",
                "ממומש מצטבר": "{:+,.2f}",
                "ירידה %": "{:.2f}%",
            }
        ).map(
            lambda v: f"color: {PALETTE['positive'] if v >= 0 else PALETTE['negative']}",
            subset=["יומי", "ממומש היום", "ממומש מצטבר"],
        ),
        use_container_width=True,
        hide_index=True,
    )

    for _, row in fleet.iterrows():
        if row["health"] in ("עצור", "מושהה"):
            st.caption(
                f"{row['client_id']} — {row['health']}: "
                f"{row['halt_reason'] or 'ללא פירוט'}"
            )


def render_accounts(fleet: pd.DataFrame, armed: bool) -> None:
    """ניהול גישה: פתיחת חשבונות, איפוס סיסמאות, שלילת גישה."""
    from app import get_account_store, get_system

    store = get_account_store()
    st.markdown("##### ניהול גישת לקוחות")
    st.caption(
        "סיסמאות נשמרות כ-hash בלבד ואינן ניתנות לשחזור. שלילת גישה מנתקת את "
        "הלקוח בטעינת הדף הבאה, לא רק בכניסה הבאה."
    )

    existing = {a.client_id: a for a in store.list_accounts()}
    profiles = get_system().registry.list_ids()

    rows = [
        {
            "מזהה לקוח": client_id,
            "יש חשבון": client_id in existing,
            "גישה פעילה": existing[client_id].active if client_id in existing else False,
            "כניסה אחרונה": (
                (existing[client_id].last_login or "—")[:19]
                if client_id in existing
                else "—"
            ),
        }
        for client_id in profiles
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()
    create_col, manage_col = st.columns(2)

    with create_col:
        st.markdown("**פתיחת חשבון חדש**")
        without_account = [c for c in profiles if c not in existing]
        if not without_account:
            st.caption("לכל פרופיל הלקוח כבר קיים חשבון גישה.")
        else:
            new_client = st.selectbox("פרופיל לקוח", without_account, key="acct_new")
            new_password = st.text_input(
                "סיסמה ראשונית", type="password", key="acct_new_pw"
            )
            st.caption("לפחות 8 תווים. מסור אותה ללקוח בערוץ נפרד — היא לא תוצג שוב.")
            if st.button("פתח חשבון", disabled=not armed, use_container_width=True):
                try:
                    store.create(new_client, new_password)
                    st.success(f"נפתח חשבון עבור {new_client}.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    with manage_col:
        st.markdown("**ניהול חשבון קיים**")
        if not existing:
            st.caption("אין עדיין חשבונות.")
        else:
            target = st.selectbox("חשבון", sorted(existing), key="acct_manage")
            account = existing[target]
            state = "פעילה" if account.active else "שלולה"
            st.caption(f"מצב גישה נוכחי: {state}")

            if account.active:
                if st.button("שלול גישה", disabled=not armed, use_container_width=True):
                    store.set_active(target, False)
                    st.success(f"הגישה של {target} נשללה.")
                    st.rerun()
            else:
                if st.button("החזר גישה", disabled=not armed, use_container_width=True):
                    store.set_active(target, True)
                    st.success(f"הגישה של {target} הוחזרה.")
                    st.rerun()

            reset_password = st.text_input(
                "סיסמה חדשה", type="password", key="acct_reset_pw"
            )
            if st.button("אפס סיסמה", disabled=not armed, use_container_width=True):
                try:
                    store.set_password(target, reset_password)
                    st.success("הסיסמה אופסה.")
                except ValueError as exc:
                    st.error(str(exc))


def render_registrations(armed: bool) -> None:
    """תור אישורים לפניות הרשמה עצמית.

    אישור הוא מה שיוצר בפועל פרופיל מסחר ומפעיל את החשבון. עד אז לפנייה אין
    שום יכולת לשלוח פקודות — היא רשומה בלבד.
    """
    from app import get_account_store, get_registration_store, get_system

    registrations = get_registration_store()
    accounts_store = get_account_store()
    system = get_system()

    st.markdown("##### פניות הרשמה")
    st.caption(
        "הרשמה עצמית יוצרת פנייה וחשבון לא פעיל בלבד. פרופיל מסחר נוצר רק "
        "באישור, עם הגדרות סיכון שמרניות שניתן להרחיב אחר כך."
    )

    pending = registrations.pending()
    if not pending:
        st.info("אין פניות ממתינות.")
    for registration in pending:
        with st.container(border=True):
            st.markdown(
                f"**{esc(registration.company)}** · `{esc(registration.client_id)}`"
            )
            details = st.columns(3)
            details[0].caption(f"איש קשר: {esc(registration.contact)}")
            details[1].caption(f"דוא\"ל: {esc(registration.email)}")
            details[2].caption(f"טלפון: {esc(registration.phone or '—')}")
            st.caption(f"התקבל: {esc(registration.submitted_at[:19])}")

            approve_col, reject_col = st.columns(2)
            with approve_col:
                if st.button(
                    "אשר ופתח פרופיל",
                    key=f"approve_{registration.client_id}",
                    disabled=not armed,
                    use_container_width=True,
                    type="primary",
                ):
                    try:
                        path = acct.provision_client_profile(
                            system.settings.clients_dir,
                            registration.client_id,
                            registration.company,
                        )
                        accounts_store.set_active(registration.client_id, True)
                        registrations.decide(registration.client_id, "approved")
                        system.registry.load(registration.client_id, refresh=True)
                        st.success(f"אושר. נוצר פרופיל {path.name}.")
                        load_fleet.clear()
                        st.rerun()
                    except (ValueError, TradesysError) as exc:
                        st.error(str(exc))
            with reject_col:
                if st.button(
                    "דחה",
                    key=f"reject_{registration.client_id}",
                    disabled=not armed,
                    use_container_width=True,
                ):
                    registrations.decide(registration.client_id, "rejected")
                    accounts_store.set_active(registration.client_id, False)
                    st.info("הפנייה נדחתה והחשבון נותר לא פעיל.")
                    st.rerun()

    history = [r for r in registrations.list_all() if r.status != "pending"]
    if history:
        st.markdown("##### פניות שטופלו")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "מזהה": r.client_id,
                        "חברה": r.company,
                        "דוא\"ל": r.email,
                        "מצב": r.status,
                        "הוחלט": r.decided_at[:19],
                    }
                    for r in history
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )


def render_lifecycle(fleet: pd.DataFrame, armed: bool) -> None:
    st.markdown("##### חיוב ומחזור חיים")
    st.caption(
        "הקפאה כותבת עצירה ליומן של הלקוח, ולכן מנהל הסיכונים חוסם כניסות מכל "
        "נקודת הרצה — לא רק מהממשק הזה. סגירת פוזיציות נשארת זמינה תמיד."
    )
    from app import get_session, invalidate, tenant_dir

    client_id = st.selectbox("לקוח", fleet["client_id"].tolist(), key="admin_lc_client")
    session = get_session(client_id)
    lifecycle = read_lifecycle(session)
    row = fleet[fleet["client_id"] == client_id].iloc[0]

    info = st.columns(4)
    info[0].metric("מצב", "פעיל" if lifecycle.active else "מושהה")
    info[1].metric("מסלול", lifecycle.plan)
    info[2].metric("שווי תיק", f"${row['equity']:,.2f}")
    info[3].metric("פוזיציות פתוחות", int(row["positions"]))
    if lifecycle.reason:
        st.caption(f"שינוי אחרון: {esc(lifecycle.reason)} · {esc(lifecycle.changed_at[:19])}")

    left, right = st.columns(2)
    with left:
        st.markdown("**השהיית לקוח**")
        reason = st.text_input("סיבה", value="מנוי לא שולם", key="admin_freeze_reason")
        flatten = st.checkbox(
            "סגור גם פוזיציות פתוחות",
            value=True,
            key="admin_flatten",
            help="רושם רווח/הפסד וכותב ניתוח לכל פוזיציה, כמו כל סגירה רגילה.",
        )
        confirm = st.text_input(f"הקלד `{client_id}` לאישור", key="admin_freeze_confirm")
        if st.button(
            "הקפא לקוח",
            disabled=not armed or not lifecycle.active or confirm != client_id,
            use_container_width=True,
            type="primary",
        ):
            try:
                closed = graceful_exit(session, f"השהיה: {reason}") if flatten else []
                freeze_tenant(session, reason)
                st.success(f"{client_id} הושהה. נסגרו {len(closed)} פוזיציות.")
                for item in closed:
                    st.caption(
                        f"{item.trade.symbol}: {item.trade.realized_pnl or 0.0:+,.2f}"
                    )
                invalidate()
                load_fleet.clear()
            except TradesysError as exc:
                st.error(str(exc))

    with right:
        st.markdown("**החזרת לקוח לפעילות**")
        plan = st.selectbox("מסלול בהפעלה", ["standard", "trial"], key="admin_plan")
        confirm_on = st.text_input(f"הקלד `{client_id}` לאישור", key="admin_unfreeze")
        if st.button(
            "הפעל מחדש",
            disabled=not armed or lifecycle.active or confirm_on != client_id,
            use_container_width=True,
        ):
            reactivate_tenant(session, plan)
            st.success(f"{client_id} הופעל מחדש במסלול {plan}.")
            invalidate()
            load_fleet.clear()
            st.rerun()

    st.divider()
    st.markdown("##### הגדרות חיוב")
    directory = tenant_dir(client_id)
    billing = acct.read_billing(directory)
    calculation = acct.calculate_fee(row["lifetime_pnl"], billing)

    settings_left, settings_right = st.columns(2)
    with settings_left:
        subscription_active = st.checkbox(
            "מנוי חודשי פעיל", value=billing.subscription_active, key="bill_sub"
        )
        monthly = st.number_input(
            "מחיר חודשי", value=float(billing.monthly_price), step=50.0, key="bill_price"
        )
    with settings_right:
        share_pct = st.number_input(
            "אחוז דמי הצלחה",
            value=float(billing.profit_share_pct),
            min_value=0.0,
            max_value=50.0,
            step=1.0,
            key="bill_share",
        )
        renews = st.text_input("מתחדש בתאריך", value=billing.renews_on, key="bill_renew")

    if st.button("שמור הגדרות חיוב", disabled=not armed):
        acct.write_billing(
            directory,
            acct.Billing(
                model=billing.model,
                subscription_active=subscription_active,
                monthly_price=monthly,
                currency=billing.currency,
                profit_share_pct=share_pct,
                high_water_mark=billing.high_water_mark,
                fees_settled=billing.fees_settled,
                renews_on=renews,
                history=billing.history,
            ),
        )
        st.success("הגדרות החיוב נשמרו.")
        st.rerun()

    fee_columns = st.columns(4)
    fee_columns[0].metric("ממומש מצטבר", f"{calculation.cumulative_realized:+,.2f}")
    fee_columns[1].metric("שיא קודם", f"{calculation.high_water_mark:+,.2f}")
    fee_columns[2].metric("רווח מעל השיא", f"{calculation.new_profit:+,.2f}")
    fee_columns[3].metric("דמי הצלחה לגבייה", f"{billing.currency}{calculation.fee_owed:,.2f}")

    if calculation.fee_owed > 0:
        st.caption(
            "רישום גבייה מעדכן את השיא הקודם, כך שאותו רווח לא ייגבה פעמיים "
            "אחרי ירידה והתאוששות."
        )
        if st.button("רשום גבייה ועדכן שיא", disabled=not armed):
            acct.settle_fee(directory, billing, calculation)
            st.success(f"נרשמה גבייה של {calculation.fee_owed:,.2f}.")
            st.rerun()
    else:
        st.caption("אין דמי הצלחה לגבייה — לא נוצר רווח חדש מעל השיא הקודם.")


def render_shadow(fleet: pd.DataFrame) -> None:
    st.markdown("##### מצב צל — ביצועי תקופת ניסיון")
    st.warning(DISCLOSURE, icon="⚠")
    st.caption(
        "לקוחות ניסיון מריצים את המערכת המלאה מול חשבון נייר, במרחב מבודד משלהם. "
        "המספרים למטה הם מה שהסימולציה הפיקה."
    )

    trials = fleet[fleet["plan"] == "trial"]
    if trials.empty:
        st.info(
            "אין לקוחות במסלול ניסיון. שייך לקוח למסלול trial בלשונית מחזור החיים "
            "כדי להתחיל הרצת צל."
        )
        return

    from app import get_session

    for _, row in trials.iterrows():
        session = get_session(row["client_id"])
        closed = session.journal.closed_trades(limit=1000)
        realized = sum(t.realized_pnl or 0.0 for t in closed)
        wins = sum(1 for t in closed if (t.realized_pnl or 0.0) > 0)

        with st.container(border=True):
            st.markdown(f"**{esc(row['name'])}** · `{esc(row['client_id'])}`")
            columns = st.columns(4)
            columns[0].metric("רווח ממומש בסימולציה", f"{realized:+,.2f}")
            columns[1].metric("עסקאות סגורות", len(closed))
            columns[2].metric(
                "אחוז הצלחה", f"{(wins / len(closed) * 100) if closed else 0:.0f}%"
            )
            columns[3].metric("פוזיציות פתוחות", int(row["positions"]))
            if realized > 0:
                st.caption(
                    f"חשבון ממומן על פרופיל זה היה מממש {realized:+,.2f} על פני "
                    f"{len(closed)} עסקאות סימולציה — לפני עמלות, החלקה ומס, "
                    f"שאף אחד מהם אינו ממודל במנוע הנייר."
                )
            else:
                st.caption(
                    f"הפרופיל הזה נמצא ב-{realized:+,.2f} על פני {len(closed)} עסקאות. "
                    f"מוצג כפי שהוא: תצוגת ניסיון שמראה רק תקופות מוצלחות "
                    f"אינה תצוגת ניסיון."
                )


def render_marketing(fleet: pd.DataFrame, armed: bool) -> None:
    st.markdown("##### סיכום לשיתוף")
    st.caption(
        "מפיק טקסט לבדיקה שלך. הוא תמיד נושא את התמונה המלאה של הלקוח — סך "
        "עסקאות, אחוז הצלחה ורווח נטו — סביב העסקאות המודגשות, וגילוי הנאות "
        "נוסע יחד איתו."
    )
    from app import get_session

    client_id = st.selectbox("לקוח", fleet["client_id"].tolist(), key="admin_share")
    top_n = st.slider("כמה עסקאות להדגיש", 1, 5, 3, key="admin_share_n")
    session = get_session(client_id)

    text, context = build_share_card(session, top_n)
    if not text:
        st.info("ללקוח זה אין עדיין עסקאות סגורות לסיכום.")
        return

    if context["net_pnl"] <= 0:
        st.warning(
            f"התיק הזה נמצא ב-{context['net_pnl']:+,.2f} נטו על פני "
            f"{context['trades']} עסקאות. פרסום עסקאות מרוויחות נבחרות מתוך תיק "
            f"מפסיד הוא בחירת דובדבנים — הסיכום למטה מציין את הנטו, ואין להסיר אותו.",
            icon="⚠",
        )

    st.code(text, language=None)
    st.download_button(
        "הורד סיכום",
        data=text.encode("utf-8"),
        file_name=f"{client_id}_summary.txt",
        mime="text/plain",
        disabled=not armed,
    )
    st.caption("שום דבר אינו מתפרסם מכאן — זו טיוטה לבדיקה ולפרסום ידני על ידך.")


def render_hive(hive: dict) -> None:
    st.markdown("##### תודעה משותפת — למידה חוצת-לקוחות")
    st.info(
        "צובר **סטטיסטיקה בלבד**: תבנית, תוצאה, מכפיל R ומשך החזקה. טקסט של "
        "ניתוחי עסקאות, נימוקים ומשוב לקוחות לעולם אינו יוצא מהמרחב של הלקוח — "
        "איחוד שלהם היה מבטל את הבטחת הבידוד שעליה המערכת נמכרת.",
        icon="🔒",
    )

    by_setup = hive["by_setup"]
    if by_setup.empty:
        st.info("אין עדיין עסקאות סגורות בצי.")
        return

    frame = hive["frame"]
    columns = st.columns(4)
    columns[0].metric("לקוחות בצבירה", hive["tenants"])
    columns[1].metric("עסקאות שנותחו", len(frame))
    columns[2].metric("אחוז הצלחה בצי", f"{frame['win'].mean() * 100:.0f}%")
    columns[3].metric("R ממוצע", f"{frame['r'].mean():+.2f}")

    display = by_setup.copy()
    display.columns = ["תבנית", "עסקאות", "אחוז הצלחה", "R ממוצע", "רווח נטו", "ימי החזקה"]
    st.dataframe(
        display.style.format(
            {
                "אחוז הצלחה": "{:.0f}%",
                "R ממוצע": "{:+.2f}",
                "רווח נטו": "{:+,.2f}",
                "ימי החזקה": "{:.1f}",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    figure = go.Figure(
        go.Bar(
            x=by_setup["setup"],
            y=by_setup["avg_r"],
            marker_color=[
                PALETTE["positive"] if v >= 0 else PALETTE["negative"]
                for v in by_setup["avg_r"]
            ],
            text=[f"n={int(n)}" for n in by_setup["trades"]],
            textposition="outside",
            textfont=dict(color=PALETTE["muted"]),
        )
    )
    figure.add_hline(y=0, line=dict(color=PALETTE["border"], width=1))
    figure.update_layout(
        height=300,
        margin=dict(l=8, r=8, t=28, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=PALETTE["muted"], size=12),
        yaxis=dict(gridcolor=PALETTE["border"], title="R ממוצע לפי תבנית"),
        xaxis=dict(gridcolor=PALETTE["border"]),
    )
    st.plotly_chart(figure, use_container_width=True)

    st.markdown("**מועמדים לשינוי כללים גלובלי**")
    st.caption(
        "המלצות בלבד — שום דבר כאן אינו משנה פרופיל לקוח. החלת כלל על לקוח "
        "היא פעולה מכוונת, כי פרופיל הסיכון שייך לו."
    )
    weakest = by_setup.sort_values("avg_r").iloc[0]
    strongest = by_setup.sort_values("avg_r").iloc[-1]
    if weakest["trades"] >= 5 and weakest["avg_r"] < 0:
        st.markdown(
            f"- `{esc(weakest['setup'])}` בממוצע **{weakest['avg_r']:+.2f}R** על פני "
            f"{int(weakest['trades'])} עסקאות בצי — שקול להוסיף אותה ל-"
            f"`forbidden_setups` בברירת המחדל ללקוחות חדשים."
        )
    if strongest["trades"] >= 5 and strongest["avg_r"] > 0:
        st.markdown(
            f"- `{esc(strongest['setup'])}` בממוצע **{strongest['avg_r']:+.2f}R** על פני "
            f"{int(strongest['trades'])} עסקאות — היתרון החזק ביותר בצבירה."
        )
    if by_setup["trades"].max() < 30:
        st.markdown(
            "- גדלי המדגם עדיין קטנים מכדי להסיק מהם. מתחת ל-30 עסקאות לתבנית "
            "הממוצעים האלה הם בעיקר רעש."
        )


# ------------------------------------------------------------------- ראשי

def render_admin() -> None:
    from app import get_system, logout

    system = get_system()
    client_ids = tuple(system.registry.list_ids())
    bust = st.session_state.get("_bust", 0)

    with st.sidebar:
        st.markdown("## ⬢ פורטל ניהול")
        st.caption("שליטה בצי הלקוחות")
        st.divider()
        armed = st.toggle(
            "אפשר פעולות ניהול",
            value=False,
            key="admin_armed",
            help="פעולות מחזור חיים משפיעות על לקוחות אמיתיים. כבוי כברירת מחדל.",
        )
        if armed:
            st.warning("פעולות הניהול פעילות.", icon="⚠")
        if st.button("רענן נתוני צי", use_container_width=True):
            load_fleet.clear()
            load_hive.clear()
            st.session_state["_bust"] = bust + 1
            st.rerun()
        st.divider()
        if st.button("התנתק", use_container_width=True):
            logout()

    st.markdown(
        '<div class="ts-hero"><div class="ts-hero-title">⬢ פורטל ניהול</div>'
        f'<div class="ts-hero-sub">{len(client_ids)} לקוחות · תצוגה חוצת-צי</div></div>',
        unsafe_allow_html=True,
    )

    fleet = load_fleet(client_ids, bust)
    hive = load_hive(client_ids, bust)

    tabs = st.tabs(
        [
            "סקירת צי",
            "דוח סוכנים",
            "פניות הרשמה",
            "ניהול גישה",
            "חיוב ומחזור חיים",
            "מצב צל",
            "ייצוא שיווקי",
            "תודעה משותפת",
        ]
    )
    with tabs[0]:
        render_fleet(fleet)
    with tabs[1]:
        render_agents(client_ids, bust)
    with tabs[2]:
        render_registrations(armed)
    with tabs[3]:
        render_accounts(fleet, armed)
    with tabs[4]:
        render_lifecycle(fleet, armed)
    with tabs[5]:
        render_shadow(fleet)
    with tabs[6]:
        render_marketing(fleet, armed)
    with tabs[7]:
        render_hive(hive)
