"""tradesys — פורטל מסחר אוטונומי. ממשק לקוח וממשק מנהל.

הרצה:

    streamlit run app.py

מודל האבטחה:
  * כל קריאה וכל פעולה מוגבלות ל-ClientSession אחד בלבד — בידוד מלא בין לקוחות.
  * סיסמאות נשמרות כ-PBKDF2-HMAC-SHA256 עם salt ייחודי. אין סיסמאות בטקסט גלוי.
  * מפתחות ברוקר מוצפנים ב-Fernet ונשמרים לכתיבה בלבד — לעולם אינם מוצגים חזרה.
  * פעולות משנות-מצב נעולות עד שהמשתמש מפעיל אותן במפורש.
  * שום דבר במסך הזה אינו עוקף את מנהל הסיכונים.

Streamlit אינו מספק אימות ברמת תשתית. השרת מאזין ל-localhost בלבד כברירת מחדל;
לפני חשיפה לרשת יש להציב לפניו proxy זהות אמיתי.
"""

from __future__ import annotations

import html
import json
import os
import sys
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

# Streamlit Cloud מזריק סודות ל-st.secrets, לא למשתני סביבה של התהליך.
# כל שאר הקוד קורא הגדרות דרך os.getenv בלבד, כך שגשר חד-פעמי כאן שומר
# על אותו קוד עובד גם מקומית (dotenv/launch.json) וגם בענן, בלי שינוי בשום
# מקום אחר. לעולם לא דורס משתנה סביבה שכבר הוגדר במפורש בתהליך.
try:
    for _key, _value in st.secrets.items():
        os.environ.setdefault(_key, str(_value))
except Exception:
    pass  # אין secrets.toml (למשל בהרצה מקומית עם launch.json) — תקין

# הייבוא חייב לבוא אחרי הגדרת sys.path
from tradesys.core.clock import trading_day  # noqa: E402
from tradesys.core.config import Settings  # noqa: E402
from tradesys.core.enums import MemoryKind  # noqa: E402
from tradesys.core.errors import TradesysError  # noqa: E402
from tradesys.core.identity import is_valid_client_id  # noqa: E402
from tradesys.orchestration.engine import TradingSystem  # noqa: E402

import accounts as acct  # noqa: E402
from tradesys.clients import automation as shared_automation  # noqa: E402

# --------------------------------------------------------------------- עיצוב

PALETTE = {
    "bg": "#0a0e1a",
    "panel": "#131a2a",
    "panel_alt": "#182033",
    "border": "#26304a",
    "text": "#eef2fa",
    "muted": "#8d97b0",
    "accent": "#4cc4f5",
    "gold": "#d4af6a",
    "positive": "#26d07c",
    "negative": "#f2545b",
    "warning": "#f5a524",
}

AUTOMATION_INTERVAL_SECONDS = 30
DEMO_CLIENT_ID = acct.DEMO_CLIENT_ID

st.set_page_config(
    page_title="tradesys · פורטל מסחר",
    page_icon="◧",
    layout="wide",
    initial_sidebar_state="auto",  # open on desktop, closed on a phone
)

CSS = f"""
<style>
  /* ---------- RTL גלובלי ---------- */
  .stApp {{
      direction: rtl;
      background: {PALETTE["bg"]};
      color: {PALETTE["text"]};
  }}
  .stApp p, .stApp span, .stApp label, .stApp li,
  .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5 {{ text-align: right; }}

  section[data-testid="stSidebar"] {{
      background: {PALETTE["panel"]};
      border-left: 1px solid {PALETTE["border"]};
      border-right: none;
      direction: rtl;
  }}
  /* A collapsed sidebar is a zero-width box; under RTL its content spilled out
     of it as a squeezed strip over the page on phones. */
  section[data-testid="stSidebar"][aria-expanded="false"] {{
      overflow: hidden; visibility: hidden;
  }}

  /* מספרים, מטבע וקוד נשארים LTR כדי שלא יישברו בפריסה הפוכה */
  div[data-testid="stMetricValue"], div[data-testid="stMetricDelta"] {{
      direction: ltr; text-align: right; font-variant-numeric: tabular-nums;
  }}
  code, pre, .stCode, .ltr {{ direction: ltr !important; text-align: left !important; }}
  div[data-testid="stDataFrame"] {{ direction: ltr; }}

  div[data-testid="stMetric"] {{
      background: {PALETTE["panel"]};
      border: 1px solid {PALETTE["border"]};
      border-radius: 14px;
      padding: 16px 18px;
  }}
  div[data-testid="stMetricLabel"] p {{
      font-size: 12px; color: {PALETTE["muted"]}; font-weight: 600;
  }}

  .ts-hero {{
      background: linear-gradient(135deg, {PALETTE["panel"]} 0%, #1b2440 100%);
      border: 1px solid {PALETTE["border"]};
      border-radius: 18px; padding: 24px 28px; margin-bottom: 18px;
  }}
  .ts-hero-title {{ font-size: 25px; font-weight: 700; margin-bottom: 4px; }}
  .ts-hero-sub {{ font-size: 13px; color: {PALETTE["muted"]}; }}

  .ts-card {{
      background: {PALETTE["panel"]};
      border: 1px solid {PALETTE["border"]};
      border-radius: 14px; padding: 18px 20px; height: 100%;
  }}
  .ts-card-label {{ font-size: 12px; color: {PALETTE["muted"]}; font-weight: 600;
      margin-bottom: 8px; }}
  .ts-card-value {{ font-size: 25px; font-weight: 700; direction: ltr;
      text-align: right; font-variant-numeric: tabular-nums; line-height: 1.2; }}
  .ts-card-sub {{ font-size: 12px; color: {PALETTE["muted"]}; margin-top: 6px; }}

  .ts-banner {{
      border-radius: 12px; padding: 13px 18px; margin-bottom: 14px;
      font-size: 13px; font-weight: 600; border: 1px solid transparent;
  }}
  .ts-banner-live {{ background: rgba(242,84,91,.13);
      border-color: {PALETTE["negative"]}; color: #ffd4d6; }}
  .ts-banner-warn {{ background: rgba(245,165,36,.12);
      border-color: {PALETTE["warning"]}; color: #ffe6b8; }}
  .ts-banner-ok {{ background: rgba(76,196,245,.10);
      border-color: {PALETTE["accent"]}; color: #c5eaff; }}

  .ts-entry {{
      background: {PALETTE["panel_alt"]}; border: 1px solid {PALETTE["border"]};
      border-right: 3px solid {PALETTE["accent"]}; border-left: none;
      border-radius: 10px; padding: 12px 15px; margin-bottom: 9px; font-size: 13px;
  }}
  .ts-entry-human {{ border-right-color: {PALETTE["warning"]}; }}
  .ts-entry-win {{ border-right-color: {PALETTE["positive"]}; }}
  .ts-entry-loss {{ border-right-color: {PALETTE["negative"]}; }}
  .ts-entry-meta {{ font-size: 11px; color: {PALETTE["muted"]};
      margin-bottom: 5px; font-weight: 600; }}

  .ts-step {{
      background: {PALETTE["panel_alt"]}; border: 1px solid {PALETTE["border"]};
      border-radius: 12px; padding: 14px 18px; margin-bottom: 10px; font-size: 14px;
  }}
  .ts-step-num {{
      display: inline-block; width: 24px; height: 24px; line-height: 24px;
      text-align: center; border-radius: 50%; background: {PALETTE["accent"]};
      color: #06121c; font-weight: 800; font-size: 13px; margin-left: 10px;
  }}
  .ts-pill {{ display: inline-block; padding: 3px 12px; border-radius: 999px;
      font-size: 11px; font-weight: 700; }}
  .stTabs [data-baseweb="tab-list"] {{ direction: rtl; gap: 6px; }}

  .st-key-home_cols div[data-testid="stHorizontalBlock"] {{ direction: rtl; flex-direction: row; }}
  .ts-chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 16px; }}
  .ts-chip {{ background: {PALETTE["panel"]}; border: 1px solid {PALETTE["border"]};
      border-radius: 999px; padding: 4px 12px; font-size: 12px; direction: ltr; }}
  .ts-vault {{ background: {PALETTE["panel"]}; border: 1px solid {PALETTE["border"]};
      border-radius: 18px; padding: 20px 22px; }}
  .ts-vault-label {{ font-size: 12px; color: {PALETTE["muted"]}; font-weight: 600;
      margin-bottom: 6px; }}
  .ts-vault-value {{ font-size: 34px; font-weight: 800; direction: ltr; text-align: right;
      font-variant-numeric: tabular-nums; line-height: 1.15; }}
  .ts-vault-bar {{ display: flex; height: 38px; border-radius: 10px; overflow: hidden;
      border: 1px solid {PALETTE["border"]}; direction: rtl; }}
  .ts-vault-legend {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px;
      margin-top: 8px; }}
  .ts-bench {{ font-size: 13px; color: {PALETTE["muted"]}; margin-top: 10px; }}
  .ts-brief {{ background: {PALETTE["panel"]}; border: 1px solid {PALETTE["border"]};
      border-right: 3px solid {PALETTE["gold"]}; border-radius: 0; padding: 14px 18px;
      margin-top: 14px; font-size: 13px; }}
  .ts-brief-title {{ font-size: 12px; color: {PALETTE["gold"]}; font-weight: 700;
      margin-bottom: 4px; }}
  .ts-brief-headline {{ font-size: 16px; font-weight: 700; margin-bottom: 6px; }}
  .ts-brief ul {{ margin: 0; padding-inline-start: 18px; color: {PALETTE["muted"]};
      line-height: 1.7; }}
  .ts-section {{ font-size: 13px; color: {PALETTE["muted"]}; font-weight: 700;
      margin-bottom: 8px; }}
  .ts-room {{ background: {PALETTE["panel"]}; border: 1px solid {PALETTE["border"]};
      border-radius: 18px; padding: 8px 16px; }}
  .ts-agent {{ display: flex; gap: 10px; padding: 9px 0; font-size: 13px;
      border-bottom: 1px solid {PALETTE["border"]}; }}
  .ts-agent:last-child {{ border-bottom: none; }}
  .ts-agent-dot {{ width: 9px; height: 9px; border-radius: 50%; margin-top: 6px;
      flex: none; }}
  .ts-agent-when {{ color: {PALETTE["muted"]}; font-size: 11px; }}
  .ts-agent-text {{ color: {PALETTE["muted"]}; font-size: 12px; line-height: 1.5; }}

  .ts-demo-bar {{
      background: linear-gradient(90deg, rgba(212,175,106,.16), rgba(212,175,106,.05));
      border: 1px solid {PALETTE["gold"]}; border-radius: 12px;
      padding: 12px 18px; margin-bottom: 14px; font-size: 13px;
      font-weight: 600; color: #f6e3bd;
  }}
  .ts-lock {{
      background: {PALETTE["panel"]}; border: 1px dashed {PALETTE["gold"]};
      border-radius: 16px; padding: 30px 34px; text-align: center;
  }}
  .ts-lock-title {{ font-size: 21px; font-weight: 700; color: {PALETTE["gold"]};
      margin-bottom: 8px; }}
  .ts-lock-body {{ font-size: 14px; color: {PALETTE["muted"]}; line-height: 1.7; }}
  .ts-price {{ font-size: 30px; font-weight: 800; direction: ltr;
      color: {PALETTE["text"]}; margin: 12px 0 4px; }}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def esc(value: object) -> str:
    """כל ערך שמוזרק ל-HTML חייב לעבור escaping.

    שמות לקוחות, סימבולים ומשוב חופשי מגיעים מקבצי הגדרה ומקלט משתמש —
    אף אחד מהם אינו markup אמין.
    """
    return html.escape(str(value), quote=True)


def ltr(value: object) -> str:
    """עוטף ערך מספרי כך שלא יישבר בפריסת RTL."""
    return f'<span dir="ltr">{esc(value)}</span>'


def money(value: float | None, currency: str = "$") -> str:
    return "—" if value is None else f"{currency}{value:,.2f}"


def signed(value: float | None) -> str:
    return "—" if value is None else f"{value:+,.2f}"


# ------------------------------------------------------------------- משאבים

@st.cache_resource(show_spinner=False)
def get_system() -> TradingSystem:
    settings = Settings.from_env(ROOT)
    return TradingSystem(settings, secrets=acct.VaultSecretResolver(settings.data_dir))


@st.cache_resource(show_spinner=False)
def get_session(client_id: str):
    """מחסנית סוכנים אחת לכל לקוח, נשמרת בין ריצות.

    ממופתחת לפי client_id, כך שיומן, זיכרון וחיבור ברוקר של לקוח אחד לעולם
    אינם מועברים ללקוח אחר.
    """
    return get_system().for_client(client_id)


@st.cache_resource(show_spinner=False)
def get_account_store() -> acct.AccountStore:
    return acct.AccountStore(Settings.from_env(ROOT).data_dir)


@st.cache_resource(show_spinner=False)
def get_registration_store() -> acct.RegistrationStore:
    return acct.RegistrationStore(Settings.from_env(ROOT).data_dir)


def invalidate() -> None:
    """מנקה מצב שמור אחרי שינוי.

    ה-Scout מקבל את ההנחיות שלו בזמן הבנייה, ולכן שינוי בהנחיות מחייב
    בנייה מחדש של ה-session.
    """
    get_session.clear()
    st.session_state["_bust"] = st.session_state.get("_bust", 0) + 1


def tenant_dir(client_id: str) -> Path:
    return Settings.from_env(ROOT).data_dir / client_id


def price_feed_label() -> str:
    """Where demo prices come from, in words a client reads in a disclosure.

    This text sits inside the simulated-performance banner, so it must match the
    actual feed: "simulated market data" is false the moment live keys are set.
    """
    if get_system().market_data_source().startswith("alpaca"):
        return "נתוני שוק אמיתיים בזמן אמת"
    return "נתוני שוק מדומים"


# --------------------------------------------------------------- מחזור חיים

def automation_enabled(session) -> bool:
    return shared_automation.automation_enabled(session.tenant.data_dir, session.client_id)


def set_automation(session, enabled: bool) -> None:
    shared_automation.set_automation(session.tenant.data_dir, enabled)


def scheduler_mode() -> bool:
    """True on the server, where a separate process runs the automated cycles."""
    return os.getenv("TRADESYS_SCHEDULER") == "1"


def tenant_suspended(session) -> tuple[bool, str]:
    from admin_panel import read_lifecycle

    lifecycle = read_lifecycle(session)
    return (not lifecycle.active), lifecycle.reason


# ---------------------------------------------------------------- מסך כניסה

def upgrade_cta(headline: str, body: str, key: str) -> None:
    """Paywall shown to demo visitors in place of live-trading features."""
    st.markdown(
        f'<div class="ts-lock">'
        f'<div class="ts-lock-title">🔒 {esc(headline)}</div>'
        f'<div class="ts-lock-body">{esc(body)}</div>'
        f'<div class="ts-price">₪499 <span style="font-size:14px">/ חודש</span></div>'
        f'<div class="ts-lock-body">בתוספת דמי הצלחה של 20% מרווחים ממומשים '
        f"מעל השיא הקודם. ללא רווח — אין דמי הצלחה.</div></div>",
        unsafe_allow_html=True,
    )
    st.markdown("")
    if st.button("פתח חשבון אמיתי", key=key, type="primary", width="stretch"):
        st.session_state["role"] = None
        st.session_state["_goto_register"] = True
        st.rerun()


def registration_form() -> None:
    """הרשמה עצמית ללקוחות B2B.

    יוצרת פנייה ממתינה וחשבון לא פעיל בלבד. פרופיל מסחר נוצר רק כשמנהל מאשר —
    טופס אנונימי לעולם לא מקים ישות שיכולה לשלוח פקודות לברוקר.
    """
    st.markdown("##### פתיחת חשבון עסקי")
    st.caption(
        "הפנייה נבדקת על ידינו לפני הפעלה. לאחר האישור תקבל גישה לחשבון נייר, "
        "וחיבור לחשבון מסחר אמיתי נעשה בשלב נפרד."
    )

    company = st.text_input("שם החברה", key="reg_company")
    contact = st.text_input("איש קשר", key="reg_contact")
    email = st.text_input("דוא\"ל", key="reg_email")
    phone = st.text_input("טלפון (לא חובה)", key="reg_phone")
    client_id = st.text_input(
        "מזהה לקוח מבוקש",
        key="reg_client",
        help="אותיות קטנות באנגלית, ספרות וקו תחתון. לדוגמה: northline_capital",
    )
    password = st.text_input("סיסמה", type="password", key="reg_password")
    password_again = st.text_input("אימות סיסמה", type="password", key="reg_password2")

    agreed = st.checkbox(
        "קראתי והבנתי שמסחר בניירות ערך כרוך בסיכון להפסד הון, "
        "ושהמערכת אינה מספקת ייעוץ השקעות.",
        key="reg_agree",
    )

    if st.button("שלח בקשה", type="primary", width="stretch"):
        store = get_account_store()
        registrations = get_registration_store()
        if not all([company.strip(), contact.strip(), email.strip(), client_id.strip()]):
            st.warning("יש למלא את כל שדות החובה.")
            return
        if password != password_again:
            st.warning("הסיסמאות אינן תואמות.")
            return
        if not agreed:
            st.warning("יש לאשר את הצהרת הסיכון.")
            return
        if not is_valid_client_id(client_id.strip()):
            st.warning(
                "מזהה הלקוח יכול להכיל רק אותיות קטנות באנגלית, ספרות וקו תחתון, "
                "להתחיל באות ולהיות באורך 3 עד 40 תווים. לדוגמה: tal_dahan"
            )
            return
        if store.get(client_id.strip()) is not None:
            st.error("מזהה הלקוח הזה כבר תפוס.")
            return
        try:
            registrations.submit(
                client_id=client_id.strip(),
                company=company,
                contact=contact,
                email=email,
                phone=phone,
            )
            # חשבון לא פעיל: לא ניתן להתחבר עד לאישור מנהל
            account = store.create(client_id.strip(), password, company.strip())
            store.set_active(account.client_id, False)
        except ValueError as exc:
            st.error(str(exc))
            return
        st.success(
            "הבקשה התקבלה. החשבון ייפתח לאחר אישור, ונעדכן אותך בדוא\"ל שהזנת."
        )
        st.info("עד אז אפשר להתנסות במערכת דרך מצב ההדגמה.", icon="💡")


def login_screen() -> None:
    st.markdown(
        '<div class="ts-hero" style="max-width:600px;margin:36px auto 18px">'
        '<div class="ts-hero-title">◧ tradesys</div>'
        '<div class="ts-hero-sub">מערכת מסחר אוטונומית מרובת-לקוחות</div></div>',
        unsafe_allow_html=True,
    )

    _, middle, _ = st.columns([1, 2, 1])
    with middle:
        st.markdown(
            '<div class="ts-demo-bar">רוצה לראות את המערכת פועלת לפני שנרשמים? '
            f"מצב ההדגמה מריץ את המנוע האמיתי על {price_feed_label()}, "
            "עם כסף מדומה — ללא הרשמה וללא חיבור ברוקר.</div>",
            unsafe_allow_html=True,
        )
        if st.button("▶ כניסה למצב הדגמה", width="stretch"):
            st.session_state["role"] = "demo"
            st.session_state["auth_client"] = DEMO_CLIENT_ID
            st.rerun()

        st.markdown("")
        # רדיו ולא לשוניות: בחירת לשונית ב-Streamlit מתאפסת בכל rerun, כך
        # שהקלדת סיסמה ולחיצת Enter היו מחזירות את המשתמש ללשונית הראשונה.
        # מפתח ב-session_state שומר את הבחירה.
        if st.session_state.pop("_goto_register", False):
            st.session_state["auth_mode"] = "פתיחת חשבון"

        mode = st.radio(
            "בחר פעולה",
            ["כניסת לקוח", "פתיחת חשבון", "כניסת מנהל"],
            horizontal=True,
            key="auth_mode",
            label_visibility="collapsed",
        )
        st.divider()

        if mode == "פתיחת חשבון":
            registration_form()

        if mode == "כניסת לקוח":
            st.markdown("##### התחברות לחשבון")
            # A form, not loose inputs: a loose text_input only reports its value on
            # Enter or blur, so a browser-autofilled field clicked straight through to
            # the button arrived empty. Form submit sends every field's current value.
            with st.form("client_login", border=False):
                client_id = st.text_input(
                    # A real id as placeholder looks like a filled field; users submitted it empty.
                    "מזהה לקוח",
                    key="login_client",
                    placeholder="הקלד כאן את מזהה הלקוח",
                    autocomplete="username",
                )
                password = st.text_input(
                    "סיסמה",
                    type="password",
                    key="login_password",
                    autocomplete="current-password",
                )
                submitted = st.form_submit_button(
                    "התחבר", width="stretch", type="primary"
                )
            if submitted:
                if not client_id.strip():
                    st.warning("לא הוזן מזהה לקוח.")
                elif not password:
                    st.warning("לא הוזנה סיסמה.")
                else:
                    # Ids are stored lowercase; "Tal_dahan" typed on a phone is the same client.
                    account = get_account_store().authenticate(
                        client_id.strip().lower(), password
                    )
                    if account is None:
                        # הודעה זהה לכל סוגי הכשל — לא חושפים אילו מזהים קיימים
                        st.error("פרטי התחברות שגויים או שהחשבון אינו פעיל.")
                    else:
                        st.session_state["role"] = "client"
                        st.session_state["auth_client"] = account.client_id
                        st.rerun()
            st.caption(
                "אין לך עדיין חשבון? בחר \"פתיחת חשבון\" למעלה. "
                "חשבונות מופעלים לאחר אישור."
            )

        if mode == "כניסת מנהל":
            st.markdown("##### כניסת מנהל מערכת")
            expected = os.getenv("TRADESYS_ADMIN_PASSCODE", "")
            if not expected:
                st.error(
                    "פורטל המנהל מושבת. יש להגדיר את משתנה הסביבה "
                    "TRADESYS_ADMIN_PASSCODE כדי להפעילו.",
                    icon="⛔",
                )
            else:
                with st.form("admin_login", border=False):
                    passcode = st.text_input(
                        "קוד גישה",
                        type="password",
                        key="login_admin",
                        autocomplete="current-password",
                    )
                    submitted = st.form_submit_button(
                        "כניסה לפורטל ניהול", width="stretch"
                    )
                if submitted:
                    import hmac

                    if passcode and hmac.compare_digest(passcode, expected):
                        st.session_state["role"] = "admin"
                        st.session_state["_admin_unlocked"] = True
                        st.rerun()
                    else:
                        st.error("קוד גישה שגוי.")

    st.markdown(
        f'<div style="text-align:center;margin-top:24px;font-size:11px;'
        f'color:{PALETTE["muted"]}">מסחר בניירות ערך כרוך בסיכון. ביצועי עבר, '
        f"לרבות ביצועי סימולציה, אינם מעידים על תוצאות עתידיות. "
        f"המערכת אינה מספקת ייעוץ השקעות.</div>",
        unsafe_allow_html=True,
    )


# -------------------------------------------------------------------- נתונים

def _floor_dict(state) -> dict | None:
    if state is None:
        return None
    return {
        "floor": state.floor,
        "base": state.base,
        "cushion": state.cushion,
        "open_risk": state.open_risk,
        "available": state.available,
        "worst_case": state.worst_case_equity,
    }


@st.cache_data(ttl=8, show_spinner=False)
def load_snapshot(client_id: str, bust: int) -> dict:
    """תצוגת קריאה בלבד של לקוח יחיד. אינה כותבת דבר."""
    del bust
    session = get_session(client_id)
    journal = session.journal
    profile = session.profile

    account = session.broker.account()
    positions = session.broker.positions()
    stats = journal.day_stats(trading_day(session.clock))
    day_start = journal.day_start_equity()
    halted, halt_reason = journal.halt_state()

    unrealized = sum(p.unrealized_pnl for p in positions)
    day_pnl = account.equity - day_start if day_start > 0 else None
    drawdown_pct = (
        max(stats.peak_equity - account.equity, 0.0) / stats.peak_equity * 100
        if stats.peak_equity > 0
        else 0.0
    )

    positions_df = pd.DataFrame(
        [
            {
                "סימבול": p.symbol,
                "כמות": p.qty,
                "כניסה": p.avg_entry_price,
                "אחרון": p.market_price,
                "שווי": p.market_value,
                "לא ממומש": p.unrealized_pnl,
                "תשואה %": (
                    (p.market_price / p.avg_entry_price - 1) * 100
                    if p.avg_entry_price
                    else 0.0
                ),
            }
            for p in positions
        ]
    )

    open_df = pd.DataFrame(
        [
            {
                "מזהה": t.trade_id,
                "סימבול": t.symbol,
                "תבנית": t.setup.value,
                "כמות": t.qty,
                "כניסה": t.entry_price,
                "סטופ": t.stop_price,
                "יעד": t.target_price,
            }
            for t in journal.open_trades()
        ]
    )

    closed = list(reversed(journal.closed_trades(limit=500)))
    closed_df = pd.DataFrame(
        [
            {
                "מזהה": t.trade_id,
                "סימבול": t.symbol,
                "תבנית": t.setup.value,
                "כניסה": t.entry_price,
                "יציאה": t.exit_price,
                "רווח/הפסד": t.realized_pnl or 0.0,
                "R": t.r_multiple(),
                "נסגר": t.closed_at,
            }
            for t in closed
        ]
    )
    if not closed_df.empty:
        closed_df["מצטבר"] = closed_df["רווח/הפסד"].cumsum()

    return {
        "display_name": profile.display_name,
        "symbols": list(profile.symbols),
        "broker_name": profile.broker.name,
        "paper": profile.broker.paper,
        "risk": {
            "ירידה מקסימלית מהשיא": f"{profile.risk.max_drawdown_pct}%",
            "הפסד יומי מקסימלי": f"{profile.risk.max_daily_loss_pct}%",
            "סיכון לעסקה": f"{profile.risk.risk_per_trade_pct}%",
            "פוזיציות פתוחות מקסימום": profile.risk.max_open_positions,
            "עסקאות ליום מקסימום": profile.risk.max_trades_per_day,
            "יחס סיכוי/סיכון מינימלי": profile.risk.min_reward_risk,
        },
        "max_open_positions": profile.risk.max_open_positions,
        "max_drawdown_pct": profile.risk.max_drawdown_pct,
        "equity": account.equity,
        "cash": account.cash,
        "unrealized": unrealized,
        "day_pnl": day_pnl,
        "realized_today": stats.realized_pnl,
        "trades_today": stats.trades_opened,
        "drawdown_pct": drawdown_pct,
        "cumulative_realized": sum(t.realized_pnl or 0.0 for t in closed),
        "floor": _floor_dict(session.risk.floor_state(account, stats)),
        "capital_base": journal.capital_base(),
        "started_on": journal.started_on(),
        "peak_equity": stats.peak_equity,
        "open_risk": journal.open_risk(),
        "halted": halted,
        "halt_reason": halt_reason,
        "position_count": len(positions),
        "positions_df": positions_df,
        "open_df": open_df,
        "closed_df": closed_df,
        "setup_perf": journal.performance_by_setup(),
        "memories": [
            {
                "kind": r.kind.value,
                "text": r.text,
                "created_at": r.created_at,
                "metadata": dict(r.metadata),
            }
            for r in session.memory.recent(limit=40)
        ],
        "directives": [
            {
                "id": d.directive_id,
                "action": d.action.value,
                "symbols": list(d.symbols),
                "setups": [s.value for s in d.setups],
                "text": d.source_text,
            }
            for d in session.reflection.active_directives()
        ],
        "taken_at": account.taken_at,
    }


@st.cache_data(ttl=30, show_spinner=False)
def load_bars(client_id: str, symbol: str, bust: int) -> pd.DataFrame:
    del bust
    session = get_session(client_id)
    bars = session.market_data.history(
        symbol, timeframe=session.profile.timeframe, limit=session.profile.lookback_bars
    )
    frame = pd.DataFrame(
        [
            {
                "ts": b.ts,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]
    )
    if not frame.empty:
        frame["sma20"] = frame["close"].rolling(20).mean()
        frame["sma50"] = frame["close"].rolling(50).mean()
    return frame


# --------------------------------------------------------------------- גרפים

def style_figure(fig: go.Figure, height: int = 360) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=PALETTE["muted"], size=12),
        hovermode="x unified",
        legend=dict(orientation="h", y=1.14, x=0, bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(gridcolor=PALETTE["border"]),
        yaxis=dict(gridcolor=PALETTE["border"]),
    )
    return fig


def equity_chart(closed_df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if closed_df.empty:
        fig.add_annotation(
            text="אין עדיין עסקאות סגורות",
            showarrow=False,
            font=dict(color=PALETTE["muted"]),
        )
        return style_figure(fig, 300)

    final = float(closed_df["מצטבר"].iloc[-1])
    colour = PALETTE["positive"] if final >= 0 else PALETTE["negative"]
    fig.add_trace(
        go.Scatter(
            x=closed_df["נסגר"],
            y=closed_df["מצטבר"],
            mode="lines",
            name="רווח מצטבר",
            line=dict(color=colour, width=2.5, shape="hv"),
            fill="tozeroy",
            fillcolor="rgba(38,208,124,.12)" if final >= 0 else "rgba(242,84,91,.12)",
        )
    )
    fig.add_hline(y=0, line=dict(color=PALETTE["border"], width=1))
    return style_figure(fig, 300)


def price_chart(
    frame: pd.DataFrame, symbol: str, overlays: list[dict], story: list[dict] | None = None
) -> go.Figure:
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.76, 0.24], vertical_spacing=0.04
    )
    fig.add_trace(
        go.Candlestick(
            x=frame["ts"],
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            name=symbol,
            increasing_line_color=PALETTE["positive"],
            decreasing_line_color=PALETTE["negative"],
            increasing_fillcolor=PALETTE["positive"],
            decreasing_fillcolor=PALETTE["negative"],
        ),
        row=1,
        col=1,
    )
    for column, colour, label in (
        ("sma20", PALETTE["accent"], "ממוצע 20"),
        ("sma50", PALETTE["gold"], "ממוצע 50"),
    ):
        fig.add_trace(
            go.Scatter(
                x=frame["ts"],
                y=frame[column],
                mode="lines",
                name=label,
                line=dict(color=colour, width=1.5),
            ),
            row=1,
            col=1,
        )
    for overlay in overlays:
        fig.add_hline(
            y=overlay["value"],
            line=dict(color=overlay["colour"], width=1.4, dash="dot"),
            annotation_text=overlay["label"],
            annotation_position="left",
            annotation_font_color=overlay["colour"],
            row=1,
            col=1,
        )
    for kind, marker in (
        ("entry", dict(symbol="triangle-up", size=15, color=PALETTE["accent"])),
        ("stop", dict(symbol="diamond", size=9, color=PALETTE["warning"])),
        ("win", dict(symbol="x", size=14, color=PALETTE["positive"])),
        ("loss", dict(symbol="x", size=14, color=PALETTE["negative"])),
    ):
        points = [point for point in story or [] if point["kind"] == kind]
        if points:
            fig.add_trace(
                go.Scatter(
                    x=[point["ts"] for point in points],
                    y=[point["price"] for point in points],
                    mode="markers",
                    marker=marker,
                    name={"entry": "כניסה", "stop": "הסטופ עלה", "win": "יציאה ברווח",
                          "loss": "יציאה בהפסד"}[kind],
                    text=[point["text"] for point in points],
                    hovertemplate="%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )
    fig.add_trace(
        go.Bar(x=frame["ts"], y=frame["volume"], name="מחזור",
               marker_color=PALETTE["border"]),
        row=2,
        col=1,
    )
    fig.update_layout(xaxis_rangeslider_visible=False)
    style_figure(fig, 500)
    fig.update_xaxes(gridcolor=PALETTE["border"], row=2, col=1)
    fig.update_yaxes(gridcolor=PALETTE["border"], row=2, col=1)
    return fig


# ---------------------------------------------------------- לשוניות הלקוח

@st.cache_data(ttl=60, show_spinner=False)
def load_market_status() -> tuple[bool, str]:
    from tradesys.data.market_clock import market_clock

    settings = Settings.from_env(ROOT)
    status = market_clock(settings.market_data_key_id, settings.market_data_secret_key).status()
    return status.is_open, status.next_open.isoformat() if status.next_open else ""


@st.cache_data(ttl=300, show_spinner=False)
def load_benchmark(client_id: str, started_on: str, bust: int) -> float | None:
    """S&P 500 (SPY) return since the account started, in percent."""
    del bust
    from datetime import date

    session = get_session(client_id)
    start = date.fromisoformat(started_on)
    days = (date.today() - start).days
    if days < 1:
        return None
    try:
        bars = session.market_data.history("SPY", timeframe="1Day", limit=days + 10)
    except Exception:  # a missing benchmark must not take the home screen down
        return None
    before = [b for b in bars if b.ts.date() <= start]
    if not before:
        return None
    return (bars[-1].close / before[-1].close - 1) * 100


@st.cache_data(ttl=60, show_spinner=False)
def load_quotes(client_id: str, bust: int) -> list[tuple[str, float, float]]:
    """(symbol, last close, change % vs the previous close) for the watchlist."""
    del bust
    session = get_session(client_id)
    quotes = []
    for symbol in session.profile.symbols:
        try:
            bars = session.market_data.history(symbol, timeframe="1Day", limit=2)
        except Exception:  # one symbol's outage must not blank the strip
            continue
        if len(bars) == 2 and bars[0].close:
            quotes.append((symbol, bars[1].close, (bars[1].close / bars[0].close - 1) * 100))
    return quotes


AGENT_ROOM = (
    ("scout", "סוכן כניסה"),
    ("offense", "סוכן התקפה"),
    ("risk", "שומר הסיכון"),
    ("execution", "סוכן ביצוע"),
    ("monitor", "סוכן יציאה"),
    ("reflection", "סוכן למידה"),
)


def vault_html(snapshot: dict) -> str:
    """The capital vault: what is locked behind the protection line, what is at risk."""
    equity = snapshot["equity"]
    base = snapshot["capital_base"] or equity
    halt_line = (snapshot["peak_equity"] or equity) * (1 - snapshot["max_drawdown_pct"] / 100)
    floor = snapshot["floor"]["floor"] if snapshot["floor"] else 0.0
    protected = min(max(halt_line, floor), equity)
    at_risk = min(snapshot["open_risk"], equity - protected)
    free = max(equity - protected - at_risk, 0.0)
    total = protected + at_risk + free or 1.0
    change = (equity / base - 1) * 100 if base else 0.0

    def segment(value: float, colour: str, label: str) -> str:
        width = value / total * 100
        text = esc(label) if width > 22 else ""
        return (
            f'<div style="width:{width:.2f}%;background:{colour};display:flex;'
            f"align-items:center;justify-content:center;font-size:12px;color:#06121c;"
            f'font-weight:700;white-space:nowrap;overflow:hidden">{text}</div>'
        )

    tone = PALETTE["positive"] if change >= 0 else PALETTE["negative"]
    return (
        '<div class="ts-vault">'
        '<div class="ts-vault-label">שווי התיק</div>'
        f'<div class="ts-vault-value">{money(equity)}</div>'
        f'<div style="color:{tone};font-size:13px;font-weight:600;margin-bottom:14px">'
        f"{ltr(f'{change:+.2f}%')} מתחילת הדרך</div>"
        '<div class="ts-vault-label">הכספת שלך</div>'
        '<div class="ts-vault-bar">'
        f'{segment(protected, PALETTE["accent"], "מוגן " + money(protected))}'
        f'{segment(at_risk, PALETTE["warning"], "בסיכון")}'
        f'{segment(free, PALETTE["positive"], "כרית")}'
        "</div>"
        '<div class="ts-vault-legend">'
        f'<span style="color:{PALETTE["accent"]}">■ מוגן {money(protected)}</span>'
        f'<span style="color:{PALETTE["warning"]}">■ בסיכון עכשיו {money(at_risk)}</span>'
        f'<span style="color:{PALETTE["positive"]}">■ כרית {money(free)}</span>'
        "</div></div>"
    )


def trading_day_start() -> datetime:
    """Midnight New York time today: the start of the trading day, in UTC."""
    from datetime import time
    from zoneinfo import ZoneInfo

    new_york = ZoneInfo("America/New_York")
    today = datetime.now(new_york).date()
    return datetime.combine(today, time.min, new_york).astimezone(timezone.utc)


def brief_html(session, snapshot: dict) -> str:
    from tradesys.orchestration.brief import daily_brief

    since = trading_day_start()
    journal = session.journal
    closed_today = [
        t for t in journal.closed_trades(limit=200) if t.closed_at and t.closed_at >= since
    ]
    brief = daily_brief(
        journal.agent_events(since),
        closed_today,
        journal.open_trades(),
        snapshot["day_pnl"],
        snapshot["equity"],
    )
    lines = "".join(f"<li>{esc(line)}</li>" for line in brief.lines)
    return (
        '<div class="ts-brief"><div class="ts-brief-title">התדריך של היום</div>'
        f'<div class="ts-brief-headline">{esc(brief.headline)}</div>'
        + (f"<ul>{lines}</ul>" if lines else "")
        + "</div>"
    )


def agents_room_html(latest: dict) -> str:
    """Each agent's latest sentence from the "why" journal, as a live team board."""
    tones = {
        "positive": PALETTE["positive"],
        "negative": PALETTE["warning"],
        "neutral": PALETTE["accent"],
    }
    rows = []
    for key, name in AGENT_ROOM:
        entry = latest.get(key)
        colour = tones[entry.tone] if entry else PALETTE["muted"]
        text = entry.text if entry else "ממתין לסבב הבא"
        when = f" · {entry.when[-5:]}" if entry else ""
        rows.append(
            '<div class="ts-agent">'
            f'<span class="ts-agent-dot" style="background:{colour}"></span>'
            f'<div><b>{esc(name)}</b><span class="ts-agent-when">{esc(when)}</span>'
            f'<div class="ts-agent-text">{esc(text)}</div></div></div>'
        )
    return f'<div class="ts-room">{"".join(rows)}</div>'


def tab_home(client_id: str, snapshot: dict, session) -> None:
    from datetime import timedelta

    from tradesys.orchestration.why import latest_by_agent

    is_open, next_open = load_market_status()
    bust = st.session_state.get("_bust", 0)
    if is_open:
        colour, label = PALETTE["positive"], "הבורסה פתוחה · סבב כל 15 דקות"
    else:
        colour = PALETTE["muted"]
        label = f"הבורסה סגורה · נפתחת ב-{next_open[11:16]} שעון ניו יורק"
    st.markdown(
        f'<span class="ts-pill" style="color:{colour};border:1px solid {colour}">'
        f"{esc(label)}</span>",
        unsafe_allow_html=True,
    )

    quotes = load_quotes(client_id, bust)
    if quotes:
        chips = "".join(
            f'<span class="ts-chip"><b>{esc(sym)}</b> {last:,.2f} '
            f'<span style="color:{PALETTE["positive"] if chg >= 0 else PALETTE["negative"]}">'
            f"{chg:+.2f}%</span></span>"
            for sym, last, chg in quotes
        )
        st.markdown(f'<div class="ts-chips">{chips}</div>', unsafe_allow_html=True)

    # Keyed so the CSS can order these columns right-to-left on wide screens;
    # on a phone they stack with the vault first.
    vault_col, side_col = st.container(key="home_cols").columns([3, 2])
    with vault_col:
        st.markdown(vault_html(snapshot), unsafe_allow_html=True)
        started = snapshot["started_on"]
        bench = load_benchmark(client_id, started.isoformat(), bust) if started else None
        base = snapshot["capital_base"] or snapshot["equity"]
        mine = (snapshot["equity"] / base - 1) * 100 if base else 0.0
        if bench is not None:
            ahead = mine - bench
            tone = PALETTE["positive"] if ahead >= 0 else PALETTE["negative"]
            verdict = "לפני" if ahead >= 0 else "מאחורי"
            st.markdown(
                '<div class="ts-bench">מול המדד (S&amp;P 500) מאז '
                f'{ltr(started.strftime("%d/%m/%Y"))}: אתה {ltr(f"{mine:+.2f}%")} · '
                f'המדד {ltr(f"{bench:+.2f}%")} · '
                f'<span style="color:{tone}">{verdict} המדד ב-'
                f'{ltr(f"{abs(ahead):.2f}%")}</span></div>',
                unsafe_allow_html=True,
            )
        st.markdown(brief_html(session, snapshot), unsafe_allow_html=True)
    with side_col:
        st.markdown('<div class="ts-section">חדר הסוכנים</div>', unsafe_allow_html=True)
        since = datetime.now(timezone.utc) - timedelta(days=7)
        st.markdown(
            agents_room_html(latest_by_agent(session.journal.agent_events(since))),
            unsafe_allow_html=True,
        )

    st.markdown("")
    overview_details(snapshot)


def overview_details(snapshot: dict) -> None:
    st.markdown("")
    left, right = st.columns([3, 2])
    with left:
        st.markdown("##### רווח ממומש מצטבר")
        st.plotly_chart(equity_chart(snapshot["closed_df"]), width="stretch")
    with right:
        st.markdown("##### ביצועים לפי תבנית")
        rows = snapshot["setup_perf"]
        if not rows:
            st.info("אין עדיין עסקאות סגורות לניתוח.")
        else:
            frame = pd.DataFrame(rows)
            figure = go.Figure(
                go.Bar(
                    x=frame["setup"],
                    y=frame["pnl"],
                    marker_color=[
                        PALETTE["positive"] if v >= 0 else PALETTE["negative"]
                        for v in frame["pnl"]
                    ],
                    text=[f"n={n}" for n in frame["n"]],
                    textposition="outside",
                    textfont=dict(color=PALETTE["muted"]),
                )
            )
            figure.add_hline(y=0, line=dict(color=PALETTE["border"], width=1))
            st.plotly_chart(style_figure(figure, 300), width="stretch")

    st.markdown("##### פוזיציות פתוחות")
    positions = snapshot["positions_df"]
    if positions.empty:
        st.info("אין פוזיציות פתוחות כרגע.")
    else:
        st.dataframe(
            positions.style.format(
                {
                    "כניסה": "${:,.2f}",
                    "אחרון": "${:,.2f}",
                    "שווי": "${:,.2f}",
                    "לא ממומש": "{:+,.2f}",
                    "תשואה %": "{:+.2f}%",
                }
            ).map(
                lambda v: f"color: {PALETTE['positive'] if v >= 0 else PALETTE['negative']}",
                subset=["לא ממומש", "תשואה %"],
            ),
            width="stretch",
            hide_index=True,
        )

    with st.expander("סטופים ויעדים בניהול המערכת"):
        open_df = snapshot["open_df"]
        if open_df.empty:
            st.caption("אין פוזיציות בניטור.")
        else:
            st.dataframe(open_df, width="stretch", hide_index=True)


def trade_story(session, symbol: str) -> list[dict]:
    """Markers for every trade in `symbol`: entry, each stop raise, and the exit."""
    import re
    from datetime import timedelta

    from tradesys.orchestration.agent_report import EXIT_NAMES, SETUP_NAMES
    from tradesys.orchestration.why import explain_rationale

    journal = session.journal
    trades = [t for t in journal.open_trades() + journal.closed_trades(limit=500)
              if t.symbol == symbol]
    story: list[dict] = []
    for trade in trades:
        setup = SETUP_NAMES.get(trade.setup.value, trade.setup.value)
        story.append({
            "kind": "entry",
            "ts": trade.opened_at,
            "price": trade.entry_price,
            "text": f"כניסה ב-{trade.entry_price:,.2f} · {setup}<br>"
                    f"{explain_rationale(trade.rationale)}<br>סטופ {trade.initial_stop or trade.stop_price:,.2f}",
        })
        if trade.closed_at and trade.exit_price is not None:
            reason = EXIT_NAMES.get(trade.exit_reason, "יציאה")
            if trade.exit_reason == "stop_loss" and (trade.realized_pnl or 0) > 0:
                reason = "סטופ נגרר שנעל רווח"
            story.append({
                "kind": "win" if (trade.realized_pnl or 0) > 0 else "loss",
                "ts": trade.closed_at,
                "price": trade.exit_price,
                "text": f"יציאה ב-{trade.exit_price:,.2f} · {reason}<br>"
                        f"{trade.realized_pnl or 0:+,.2f}$ ({trade.r_multiple():+.2f}R)",
            })
    since = datetime.now(timezone.utc) - timedelta(days=400)
    for event in journal.agent_events(since):
        if event["agent"] == "monitor" and event["kind"] == "stop_raised" and event["symbol"] == symbol:
            prices = re.findall(r"[\d.]+", event["detail"].split("stop", 1)[-1])
            if len(prices) >= 2:
                story.append({
                    "kind": "stop",
                    "ts": datetime.fromisoformat(event["created_at"]),
                    "price": float(prices[1]),
                    "text": f"הסטופ עלה מ-{prices[0]} ל-{prices[1]}",
                })
    return story


def tab_charts(client_id: str, snapshot: dict, session) -> None:
    chosen = st.selectbox("בחר נייר ערך", snapshot["symbols"], key="chart_symbol")
    frame = load_bars(client_id, chosen, st.session_state.get("_bust", 0))
    if frame.empty:
        st.warning(f"לא התקבלו נתונים עבור {chosen}.")
        return

    overlays: list[dict] = []
    open_df = snapshot["open_df"]
    if not open_df.empty:
        for _, row in open_df[open_df["סימבול"] == chosen].iterrows():
            overlays.append(
                {"value": row["כניסה"], "colour": PALETTE["accent"], "label": "כניסה"}
            )
            overlays.append(
                {"value": row["סטופ"], "colour": PALETTE["negative"], "label": "סטופ"}
            )
            if row["יעד"]:
                overlays.append(
                    {"value": row["יעד"], "colour": PALETTE["positive"], "label": "יעד"}
                )

    last = float(frame["close"].iloc[-1])
    previous = float(frame["close"].iloc[-2]) if len(frame) > 1 else last
    change = (last / previous - 1) * 100 if previous else 0.0

    metrics = st.columns(4)
    metrics[0].metric("מחיר אחרון", f"${last:,.2f}")
    metrics[1].metric("שינוי", f"{change:+.2f}%")
    sma20 = frame["sma20"].iloc[-1]
    metrics[2].metric("ממוצע 20", f"${sma20:,.2f}" if pd.notna(sma20) else "—")
    metrics[3].metric("נרות", f"{len(frame):,}")

    # A fill outside the candles' range means the journal and this price feed
    # disagree (a history recorded on another feed); drawing it would mislead.
    low, high = frame["low"].min() * 0.9, frame["high"].max() * 1.1
    story = [p for p in trade_story(session, chosen) if low <= p["price"] <= high]
    st.plotly_chart(price_chart(frame, chosen, overlays, story), width="stretch")
    if overlays:
        st.caption("הקווים המקווקווים הם רמות הכניסה, הסטופ והיעד של הפוזיציה שלך.")
    if story:
        st.caption("משולש: כניסה · יהלום: הסטופ עלה · איקס: יציאה. העבר את העכבר לקבלת ההסבר.")

    from datetime import timedelta

    from tradesys.orchestration.why import timeline

    st.markdown(f"##### הסיפור של {chosen}")
    since = datetime.now(timezone.utc) - timedelta(days=90)
    render_why(timeline(session.journal.agent_events(since), chosen), limit=25)


def render_why(entries, limit: int = 40) -> None:
    """The "why" journal: one line per agent decision, newest first."""
    tones = {
        "positive": PALETTE["positive"],
        "negative": PALETTE["negative"],
        "neutral": PALETTE["border"],
    }
    if not entries:
        st.info("עוד אין החלטות ביומן. הוא מתמלא בכל סבב מסחר, כשהבורסה פתוחה.")
    for entry in entries[:limit]:
        symbol = f"{esc(entry.symbol)} · " if entry.symbol else ""
        st.markdown(
            f'<div class="ts-entry" style="border-right-color:{tones[entry.tone]}">'
            f'<div class="ts-entry-meta">{symbol}{ltr(entry.when)}</div>'
            f"{esc(entry.text)}</div>",
            unsafe_allow_html=True,
        )


def tab_agents(snapshot: dict, session, armed: bool) -> None:
    from datetime import timedelta

    from tradesys.orchestration.why import timeline

    st.markdown("##### למה הרובוט עשה את מה שעשה")
    st.caption(
        "כל החלטה של הסוכנים בשבועיים האחרונים, בשפה פשוטה: מה זוהה, מה אושר, "
        "מה נחסם ולמה, ואיך כל עסקה נסגרה."
    )
    since = datetime.now(timezone.utc) - timedelta(days=14)
    render_why(timeline(session.journal.agent_events(since)), limit=30)
    st.divider()

    st.markdown("##### מה הסוכנים רואים עכשיו")
    st.caption(
        "הרצה יבשה של הסקאוט ומנהל הסיכונים. שום פקודה אינה נשלחת לברוקר — "
        "זו תצוגה של ההחלטות שהיו מתקבלות כרגע, כולל כל סיבות הדחייה."
    )
    if st.button("סרוק הזדמנויות"):
        try:
            with st.spinner("סורק את רשימת המעקב…"):
                account = session.broker.account()
                positions = session.broker.positions()
                stats = session.journal.day_stats(trading_day(session.clock))
                verdicts = [
                    (c, session.risk.evaluate(c, account, positions, stats))
                    for c in session.scout.scan()
                ]
            st.session_state["_verdicts"] = [
                {
                    "symbol": c.symbol,
                    "setup": c.setup.value,
                    "entry": c.entry,
                    "stop": c.stop,
                    "target": c.target,
                    "confidence": c.confidence,
                    "rationale": c.rationale,
                    "approved": d.approved,
                    "qty": d.qty,
                    "reasons": list(d.reasons),
                }
                for c, d in verdicts
            ]
        except TradesysError as exc:
            st.error(f"הסריקה נכשלה: {exc}")

    for verdict in st.session_state.get("_verdicts", []):
        colour = PALETTE["positive"] if verdict["approved"] else PALETTE["negative"]
        label = f"אושר · {verdict['qty']} יחידות" if verdict["approved"] else "נדחה"
        levels = (
            f'כניסה {verdict["entry"]:.2f} · '
            f'סטופ {verdict["stop"]:.2f} · '
            f'יעד {verdict["target"]:.2f}'
        )
        st.markdown(
            f'<div class="ts-entry" style="border-right-color:{colour}">'
            f'<div class="ts-entry-meta">{esc(verdict["symbol"])} · '
            f'{esc(verdict["setup"])} · ביטחון {verdict["confidence"]:.2f} · '
            f'<span style="color:{colour}">{esc(label)}</span></div>'
            f"{ltr(levels)}<br>"
            f'<span style="color:{PALETTE["muted"]}">{esc(verdict["rationale"])}</span>'
            f"</div>",
            unsafe_allow_html=True,
        )
        if verdict["reasons"]:
            with st.expander(f"נימוקי מנהל הסיכונים — {verdict['symbol']}"):
                for reason in verdict["reasons"]:
                    st.markdown(f"- {esc(reason)}")

    st.divider()
    left, right = st.columns([3, 2])

    with left:
        st.markdown("##### יומן הלמידה של המערכת")
        options = {
            "הכול": None,
            "ניתוח עסקאות": MemoryKind.TRADE_POSTMORTEM,
            "המשוב שלי": MemoryKind.HUMAN_FEEDBACK,
            "תובנות מצטברות": MemoryKind.SESSION_LESSON,
        }
        chosen = st.radio(
            "סינון", list(options), horizontal=True, label_visibility="collapsed"
        )
        wanted = options[chosen]
        entries = [
            m for m in snapshot["memories"] if wanted is None or m["kind"] == wanted.value
        ]
        if not entries:
            st.info("אין עדיין רשומות ביומן.")
        for entry in entries[:20]:
            css = "ts-entry"
            author = ""
            if entry["kind"] == MemoryKind.HUMAN_FEEDBACK.value:
                css += " ts-entry-human"
                if entry["metadata"].get("source") == "operator":
                    author = "הערת מנהל · "
            elif entry["metadata"].get("outcome") == "win":
                css += " ts-entry-win"
            elif entry["metadata"].get("outcome") == "loss":
                css += " ts-entry-loss"
            when = (
                entry["created_at"].strftime("%Y-%m-%d %H:%M")
                if entry["created_at"]
                else ""
            )
            st.markdown(
                f'<div class="{css}"><div class="ts-entry-meta">{esc(author)}{ltr(when)}</div>'
                f'{esc(entry["text"])}</div>',
                unsafe_allow_html=True,
            )

    with right:
        st.markdown("##### הנחיות פעילות")
        st.caption("הנחיות שנוצרו מהמשוב שלך. הנחיית הימנעות חוסמת עסקאות בפועל.")
        if not snapshot["directives"]:
            st.info("אין הנחיות — המערכת פועלת לפי הכללים הבסיסיים.")
        for directive in snapshot["directives"]:
            scope = ", ".join(directive["symbols"] or directive["setups"]) or "כללי"
            colour = (
                PALETTE["negative"]
                if directive["action"] == "avoid"
                else PALETTE["positive"]
                if directive["action"] == "prioritize"
                else PALETTE["muted"]
            )
            st.markdown(
                f'<div class="ts-entry" style="border-right-color:{colour}">'
                f'<div class="ts-entry-meta">#{esc(directive["id"])} · '
                f'<span style="color:{colour}">{esc(directive["action"])}</span> · '
                f'{esc(scope)}</div>{esc(directive["text"])}</div>',
                unsafe_allow_html=True,
            )
            if st.button("בטל הנחיה", key=f"retire_{directive['id']}", disabled=not armed):
                session.reflection.retire_directive(int(directive["id"]))
                invalidate()
                st.rerun()

        st.markdown("##### למד את המערכת")
        feedback = st.text_area(
            "משוב",
            placeholder="עסקה 45 על RIVN הייתה מצוינת, תעדף את התבנית הזו",
            label_visibility="collapsed",
        )
        if st.button("שלח משוב", disabled=not armed, width="stretch"):
            if not feedback.strip():
                st.warning("לא הוזן טקסט.")
            else:
                try:
                    receipt = session.reflection.ingest_feedback(feedback)
                    st.success(f"נשמר. תורגם להנחיה: {receipt.describe()}")
                    invalidate()
                    st.rerun()
                except (TradesysError, ValueError) as exc:
                    st.error(str(exc))

    st.markdown("##### עסקאות שנסגרו")
    closed = snapshot["closed_df"]
    if closed.empty:
        st.caption("אין עדיין עסקאות סגורות.")
    else:
        st.dataframe(
            closed.iloc[::-1].style.format(
                {
                    "כניסה": "${:,.2f}",
                    "יציאה": "${:,.2f}",
                    "רווח/הפסד": "{:+,.2f}",
                    "R": "{:+.2f}",
                    "מצטבר": "{:+,.2f}",
                }
            ),
            width="stretch",
            hide_index=True,
        )


def tab_broker(client_id: str, demo: bool = False) -> None:
    """אשף חיבור ברוקר.

    שלושה עקרונות: המפתחות נבדקים מול הברוקר לפני שמירה, נשמרים מוצפנים,
    ולעולם אינם מוצגים חזרה — רק מזהה ממוסך.
    """
    if demo:
        upgrade_cta(
            "חיבור ברוקר זמין ללקוחות רשומים",
            f"במצב הדגמה המערכת רצה על {price_feed_label()} עם כסף מדומה. "
            "חיבור חשבון מסחר אמיתי נפתח לאחר פתיחת חשבון ואישור.",
            key="cta_broker",
        )
        return

    st.markdown("##### חיבור חשבון המסחר שלך")
    vault = acct.BrokerVault(tenant_dir(client_id))
    existing = vault.metadata()

    if existing:
        st.markdown(
            f'<div class="ts-banner ts-banner-ok">✓ מחובר לברוקר '
            f'{esc(existing.broker)} · מפתח {ltr(existing.key_id_masked)} · '
            f'{"חשבון נייר" if existing.paper else "חשבון אמיתי"}</div>',
            unsafe_allow_html=True,
        )
        info = st.columns(3)
        info[0].metric("ברוקר", existing.broker)
        info[1].metric("סוג חשבון", "נייר" if existing.paper else "אמיתי")
        info[2].metric("אומת בתאריך", (existing.verified_at or "—")[:10])
        st.caption(
            "מטעמי אבטחה המפתח הסודי אינו ניתן לצפייה לאחר השמירה. "
            "כדי להחליף מפתחות יש לנתק ולחבר מחדש."
        )
        if st.button("נתק חשבון ברוקר"):
            vault.clear()
            invalidate()
            st.rerun()
        return

    st.markdown(
        '<div class="ts-step"><span class="ts-step-num">1</span>'
        'היכנס לחשבון Alpaca שלך ופתח את המסך <span dir="ltr">API Keys</span>.</div>'
        '<div class="ts-step"><span class="ts-step-num">2</span>'
        "צור מפתח חדש. יוצגו שני ערכים — מזהה מפתח וסוד. "
        "שים לב: הסוד מוצג פעם אחת בלבד.</div>"
        '<div class="ts-step"><span class="ts-step-num">3</span>'
        "העתק את שניהם לשדות שלמטה. נבדוק אותם מול הברוקר לפני השמירה, "
        "כך שטעות הקלדה תתגלה מיד.</div>",
        unsafe_allow_html=True,
    )

    try:
        acct._fernet()
        vault_ready, vault_error = True, ""
    except acct.VaultLocked as exc:
        vault_ready, vault_error = False, str(exc)

    if not vault_ready:
        st.error(
            "כספת המפתחות נעולה ולכן לא ניתן לשמור פרטי ברוקר. "
            f"פנה למנהל המערכת. פירוט טכני: {vault_error}",
            icon="🔒",
        )
        return

    st.warning(
        "התחל תמיד בחשבון נייר. חבר חשבון אמיתי רק אחרי שראית את המערכת "
        "פועלת כמצופה לאורך זמן.",
        icon="⚠",
    )

    paper = st.radio(
        "סוג חשבון",
        [True, False],
        format_func=lambda v: "חשבון נייר — מומלץ" if v else "חשבון אמיתי — כסף אמיתי",
        horizontal=True,
    )
    key_id = st.text_input("מזהה מפתח (API Key ID)", key="broker_key")
    secret_key = st.text_input("מפתח סודי (Secret Key)", type="password", key="broker_secret")

    if not paper:
        st.error(
            "אתה עומד לחבר חשבון אמיתי. המערכת תוכל לבצע פעולות בכסף אמיתי, "
            "בכפוף למגבלות הסיכון שבפרופיל שלך.",
            icon="⛔",
        )
        confirm = st.text_input("הקלד `אני מאשר` כדי להמשיך", key="broker_confirm")
        allowed = confirm.strip() == "אני מאשר"
    else:
        allowed = True

    if st.button(
        "בדוק ושמור חיבור", type="primary", width="stretch", disabled=not allowed
    ):
        if not key_id.strip() or not secret_key.strip():
            st.warning("יש למלא את שני השדות.")
            return
        with st.spinner("מאמת מול הברוקר…"):
            ok, message = acct.verify_alpaca_credentials(
                key_id.strip(), secret_key.strip(), paper
            )
        if not ok:
            st.error(f"האימות נכשל. {message}")
            return
        try:
            entry = vault.store(
                broker="alpaca",
                key_id=key_id.strip(),
                secret_key=secret_key.strip(),
                paper=paper,
                account_label=message,
            )
        except acct.VaultLocked as exc:
            st.error(str(exc))
            return
        st.success(
            f"החיבור אומת ונשמר מוצפן. מפתח {entry.key_id_masked}. "
            "כדי להתחיל לסחור בחשבון זה יש לעדכן את פרופיל הלקוח לברוקר alpaca."
        )
        invalidate()
        st.rerun()


def tab_billing(client_id: str, snapshot: dict, demo: bool = False) -> None:
    directory = tenant_dir(client_id)
    billing = acct.read_billing(directory)
    calculation = acct.calculate_fee(snapshot["cumulative_realized"], billing)

    st.markdown("##### מסלול החיוב שלך")
    left, right = st.columns(2)

    with left:
        status = "פעיל" if billing.subscription_active else "לא פעיל"
        colour = PALETTE["positive"] if billing.subscription_active else PALETTE["negative"]
        renews = f" · מתחדש ב-{billing.renews_on}" if billing.renews_on else ""
        st.markdown(
            f'<div class="ts-card"><div class="ts-card-label">מנוי חודשי</div>'
            f'<div class="ts-card-value" style="color:{colour}">{esc(status)}</div>'
            f'<div class="ts-card-sub">'
            f"{ltr(f'{billing.currency}{billing.monthly_price:,.0f}')} "
            f'לחודש{esc(renews)}</div></div>',
            unsafe_allow_html=True,
        )

    with right:
        st.markdown(
            f'<div class="ts-card"><div class="ts-card-label">דמי הצלחה</div>'
            f'<div class="ts-card-value" style="color:{PALETTE["gold"]}">'
            f"{billing.profit_share_pct:.0f}%</div>"
            f'<div class="ts-card-sub">מהרווחים הממומשים מעל השיא הקודם</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("")
    st.markdown("##### חישוב דמי ההצלחה")
    metrics = st.columns(4)
    metrics[0].metric("רווח ממומש מצטבר", signed(calculation.cumulative_realized))
    metrics[1].metric("שיא קודם", signed(calculation.high_water_mark))
    metrics[2].metric("רווח חדש מעל השיא", signed(calculation.new_profit))
    metrics[3].metric("לתשלום כעת", money(calculation.fee_owed, billing.currency))

    if calculation.below_hwm_by > 0:
        st.markdown(
            f'<div class="ts-banner ts-banner-ok">אינך מחויב בדמי הצלחה כעת. '
            f"התיק נמצא {ltr(f'{calculation.below_hwm_by:,.2f}')} מתחת לשיא הקודם. "
            f"דמי הצלחה ייגבו רק על רווח חדש מעבר לשיא — לא על התאוששות.</div>",
            unsafe_allow_html=True,
        )
    elif calculation.fee_owed > 0:
        st.markdown(
            f'<div class="ts-banner ts-banner-warn">דמי הצלחה לתשלום: '
            f"{ltr(f'{billing.currency}{calculation.fee_owed:,.2f}')} — "
            f"{billing.profit_share_pct:.0f}% מתוך "
            f"{ltr(f'{calculation.new_profit:,.2f}')} רווח חדש מעל השיא.</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="ts-banner ts-banner-ok">אין דמי הצלחה לתשלום — '
            "טרם נוצר רווח ממומש מעל השיא.</div>",
            unsafe_allow_html=True,
        )

    with st.expander("איך מחושבים דמי ההצלחה?"):
        st.markdown(
            f"""
דמי ההצלחה נגבים **רק על רווח חדש מעל השיא הקודם** (High-Water Mark).

דוגמה בשיעור {billing.profit_share_pct:.0f}%:

1. הרווח המצטבר מגיע ל-1,000 → נגבים דמי הצלחה על 1,000, והשיא מתעדכן ל-1,000.
2. התיק יורד ל-200 → **אין חיוב**. השיא נשאר 1,000.
3. הרווח חוזר ל-1,000 → **אין חיוב**, כי לא נוצר רווח חדש מעל השיא.
4. הרווח עולה ל-1,400 → נגבים דמי הצלחה על 400 בלבד.

בלי מנגנון זה היית מחויב פעמיים על אותו רווח לאחר התאוששות מירידה.
"""
        )

    if billing.history:
        st.markdown("##### היסטוריית חיובים")
        st.dataframe(
            pd.DataFrame(billing.history), width="stretch", hide_index=True
        )

    st.caption(
        "הסכומים מוצגים למעקב בלבד ואינם מהווים חשבונית או דרישת תשלום. "
        "הגבייה מתבצעת מחוץ למערכת."
    )

    if demo:
        st.divider()
        upgrade_cta(
            "כך ייראה החיוב שלך",
            "המספרים למעלה מחושבים על תוצאות ההדגמה. בחשבון אמיתי החישוב "
            "זהה, על הרווחים הממומשים שלך בפועל.",
            key="cta_billing",
        )


def risk_gauge(value: float, maximum: float, title: str, bands: list[tuple[float, str]]) -> go.Figure:
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=value,
            number={"font": {"size": 34, "color": PALETTE["text"]}},
            title={"text": title, "font": {"size": 14, "color": PALETTE["muted"]}},
            gauge={
                "axis": {"range": [0, maximum], "tickcolor": PALETTE["muted"]},
                "bar": {"color": PALETTE["text"], "thickness": 0.25},
                "bgcolor": "rgba(0,0,0,0)",
                "borderwidth": 0,
                "steps": [
                    {"range": [low, high], "color": color}
                    for (low, color), (high, _) in zip(bands, bands[1:] + [(maximum, "")])
                ],
            },
        )
    )
    fig.update_layout(
        height=230,
        margin=dict(l=20, r=20, t=40, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color=PALETTE["muted"]),
    )
    return fig


def locked_hint(armed: bool) -> None:
    """Say why a button is greyed out: a disabled control explains nothing on its own."""
    if not armed:
        st.caption("🔒 כדי לשמור, הפעל את \"אפשר פעולות\" בתפריט הצדדי (» בראש המסך).")


def control_panel(client_id: str, snapshot: dict, session, armed: bool) -> str:
    """How the client runs the robot: autopilot or own settings, and how much money."""
    from tradesys.brokers.paper import PaperBroker
    from tradesys.clients.tracks import read_control, write_control

    st.markdown("##### איך הרובוט עובד בשבילך")
    control = read_control(session.tenant.data_dir)
    equity = snapshot["equity"]
    labels = {"auto": "🤖 טייס אוטומטי (מומלץ)", "custom": "🎛 לפי ההגדרות שלי"}
    mode = st.radio(
        "מצב",
        list(labels),
        index=0 if control and control.mode == "auto" else 1,
        format_func=labels.get,
        horizontal=True,
        key=f"control_mode_{client_id}",
        label_visibility="collapsed",
    )
    if mode == "auto":
        st.caption(
            "הרובוט מנהל הכול לבד. הוא מתאים את האגרסיביות לשוק: 0.75% סיכון לעסקה כשהשוק "
            "בעלייה (מעל ממוצע 200 הימים), ו-0.25% כשהשוק חלש. מגבלות הבטיחות קבועות ברמה "
            "\"מאוזן\": רצפה ב-90% ועצירה בירידה של 10%."
        )
    else:
        st.caption("אתה בוחר את רמת הסיכון במד שלמטה, והרובוט פועל לפיה.")

    whole = st.checkbox(
        "הרובוט רשאי להשקיע את כל החשבון",
        value=not (control and control.allocation),
        key=f"control_whole_{client_id}",
    )
    allocation = None
    if not whole:
        allocation = st.number_input(
            "כמה מהחשבון הרובוט רשאי להשקיע ($)",
            min_value=1_000.0,
            max_value=max(float(equity), 1_000.0),
            value=float(min(control.allocation, equity)) if control and control.allocation
            else float(round(equity / 2, -3)),
            step=1_000.0,
            key=f"control_alloc_{client_id}",
        )
        st.caption(
            f"הרובוט לא יחזיק פוזיציות ביותר מ-{money(allocation)}, וגודל כל עסקה יחושב "
            "לפי הסכום הזה ולא לפי כל החשבון."
        )

    changed = (
        control is None
        or control.mode != mode
        or (control.allocation or None) != (allocation or None)
    )
    locked_hint(armed)
    if st.button("שמור את ההגדרות", type="primary", disabled=not armed or not changed,
                 width="stretch", key=f"control_save_{client_id}"):
        write_control(session.tenant.data_dir, mode, allocation, equity)
        invalidate()
        st.rerun()

    my_stocks(client_id, session, armed)

    if isinstance(session.broker, PaperBroker):
        with st.expander("הוספת כסף לחשבון"):
            amount = st.number_input(
                "סכום להפקדה ($)", min_value=100.0, value=10_000.0, step=1_000.0,
                key=f"deposit_{client_id}",
            )
            st.caption("בחשבון הדמו הכסף מדומה. הפקדה לא נספרת כרווח, ולא משנה את מדידת הירידה.")
            if st.button("הפקד", disabled=not armed, key=f"deposit_go_{client_id}"):
                session.broker.deposit(amount)
                session.journal.record_deposit(amount)
                st.success(f"הופקדו {money(amount)}.")
                invalidate()
                st.rerun()
    else:
        st.caption("בחשבון אמיתי מפקידים כסף אצל הברוקר, והרובוט רואה אותו בסבב הבא.")
    st.divider()
    return control.mode if control else "custom"


@st.cache_resource(show_spinner=False)
def get_asset_directory():
    import httpx

    from tradesys.data.asset_search import AssetDirectory

    settings = Settings.from_env(ROOT)

    def fetch() -> list[dict]:
        response = httpx.get(
            "https://paper-api.alpaca.markets/v2/assets",
            params={"status": "active", "asset_class": "us_equity"},
            headers={
                "APCA-API-KEY-ID": settings.market_data_key_id,
                "APCA-API-SECRET-KEY": settings.market_data_secret_key,
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    return AssetDirectory(settings.data_dir / "_platform", fetch)


def add_pick(session, picks: list, ticker: str) -> None:
    """Validate a ticker against market data, then save it as a pick."""
    from tradesys.clients.picks import Pick, normalize, write_picks
    from tradesys.core.errors import ConfigError

    ticker = normalize(ticker)
    bars = session.market_data.history(ticker, timeframe="1Day", limit=60)
    if len(bars) < 60:
        raise ConfigError(f"לא נמצאו מספיק נתוני מסחר עבור {ticker}")
    write_picks(session.tenant.data_dir, [*picks, Pick(ticker)])


def my_stocks(client_id: str, session, armed: bool) -> None:
    """Stocks the client wants the robot to watch, on top of the leaders it picks."""
    from tradesys.clients.picks import MAX_PICKS, read_picks, write_picks
    from tradesys.core.errors import ConfigError

    st.markdown("###### המניות שלי")
    picks = read_picks(session.tenant.data_dir)
    hunting = session.profile.leaders_count > 0
    st.caption(
        ("הרובוט בוחר בעצמו את המובילות של השוק, ובנוסף בודק בכל סבב את המניות שתוסיף כאן. "
         if hunting else "בנוסף לרשימת המעקב, הרובוט יבדוק בכל סבב את המניות שתוסיף כאן. ")
        + "הוא קונה רק כשיש נקודת כניסה טובה ושומר הסיכון מאשר."
    )

    query = st.text_input(
        "חיפוש",
        placeholder="חפש מניה, קרן סל או מדד: אנבידיה, TEVA, נאסד\"ק, S&P 500, זהב…",
        key=f"pick_search_{client_id}",
        label_visibility="collapsed",
    )
    if query.strip():
        try:
            hits = get_asset_directory().search(query, limit=8)
        except Exception:  # the broker's listing being down must not break the tab
            hits = []
            st.error("החיפוש לא זמין כרגע. נסה שוב בעוד דקה.")
        if not hits:
            st.caption("לא נמצאו תוצאות. נסה שם באנגלית או את סימול המניה.")
        for hit in hits:
            info_col, button_col = st.columns([4, 1])
            note = f" · {hit.note}" if hit.note else ""
            info_col.markdown(
                f'<div class="ts-agent"><div><b>{esc(hit.symbol)}</b> · {esc(hit.kind)}{esc(note)}'
                f'<div class="ts-agent-text">{esc(hit.name)}</div></div></div>',
                unsafe_allow_html=True,
            )
            chosen = hit.symbol in {p.symbol for p in picks}
            if button_col.button(
                "נבחרה" if chosen else "הוסף",
                disabled=not armed or chosen or len(picks) >= MAX_PICKS,
                width="stretch",
                key=f"pick_add_{client_id}_{hit.symbol}",
            ):
                try:
                    add_pick(session, picks, hit.symbol)
                    invalidate()
                    st.rerun()
                except ConfigError as exc:
                    st.error(str(exc))
                except Exception:  # a market-data failure must not break the tab
                    st.error("לא הצלחנו לבדוק את המניה כרגע. נסה שוב בעוד דקה.")
    if len(picks) >= MAX_PICKS:
        st.caption(f"הגעת למקסימום של {MAX_PICKS} מניות. הסר אחת כדי להוסיף אחרת.")
    locked_hint(armed)

    if picks:
        st.caption(
            "⭐ עדיפות: הרובוט בודק את המניה לפני הרעיונות שלו. "
            "✋ לשאול אותי: כשהרובוט רוצה לקנות, הוא מבקש את אישורך במקום לקנות. "
            "אפשר גם לאשר מראש ליום המסחר הקרוב, והוא יקנה בלי לשאול."
        )
    for pick in picks:
        name_col, star_col, ask_col, remove_col = st.columns([2, 2, 2, 1])
        name_col.markdown(f'<span class="ts-chip"><b>{esc(pick.symbol)}</b></span>',
                          unsafe_allow_html=True)
        priority = star_col.checkbox(
            "⭐ עדיפות", value=pick.priority, disabled=not armed,
            key=f"pick_star_{client_id}_{pick.symbol}",
        )
        ask_first = ask_col.checkbox(
            "✋ לשאול אותי", value=pick.ask_first, disabled=not armed,
            key=f"pick_ask_{client_id}_{pick.symbol}",
        )
        if (priority, ask_first) != (pick.priority, pick.ask_first):
            write_picks(
                session.tenant.data_dir,
                [replace(p, priority=priority, ask_first=ask_first) if p.symbol == pick.symbol
                 else p for p in picks],
            )
            invalidate()
            st.rerun()
        if remove_col.button("הסר", disabled=not armed, key=f"pick_rm_{client_id}_{pick.symbol}",
                             width="stretch"):
            write_picks(session.tenant.data_dir, [p for p in picks if p.symbol != pick.symbol])
            invalidate()
            st.rerun()
        if pick.ask_first:
            standing_approval(client_id, session, picks, pick, armed)


def standing_approval(client_id: str, session, picks: list, pick, armed: bool) -> None:
    """Approve an "ask first" pick ahead of time, for the coming trading day."""
    from tradesys.clients.picks import approval_day, write_picks

    day = approval_day(datetime.now(timezone.utc))
    label = day.strftime("%d/%m")

    def save(**changes) -> None:
        write_picks(
            session.tenant.data_dir,
            [replace(p, **changes) if p.symbol == pick.symbol else p for p in picks],
        )
        invalidate()
        st.rerun()

    if pick.preapproved_on == day.isoformat():
        limit = f", עד {money(pick.max_amount)}" if pick.max_amount else ""
        text_col, cancel_col = st.columns([3, 1])
        text_col.success(
            f"✅ {pick.symbol} מאושרת מראש ליום המסחר {label}{limit}. "
            "אם תהיה נקודת כניסה טובה, הרובוט יקנה בלי לשאול.",
        )
        if cancel_col.button("בטל אישור", disabled=not armed,
                             key=f"preapprove_off_{client_id}_{pick.symbol}",
                             width="stretch"):
            save(preapproved_on="", max_amount=None)
        return

    amount_col, button_col = st.columns([2, 2])
    amount = amount_col.number_input(
        f"סכום מקסימלי ל-{pick.symbol} ($, 0 = לפי שומר הסיכון)",
        min_value=0.0, value=0.0, step=500.0,
        key=f"preapprove_amount_{client_id}_{pick.symbol}",
    )
    if button_col.button(f"✅ אשר מראש ל-{label}", disabled=not armed,
                         key=f"preapprove_on_{client_id}_{pick.symbol}",
                         width="stretch"):
        save(preapproved_on=day.isoformat(), max_amount=amount or None)


def proposals_banner(client_id: str, session, armed: bool) -> None:
    """The robot's requests to buy an "ask first" pick, with approve and decline."""
    from tradesys.clients.proposals import pending

    waiting = pending(session.tenant.data_dir, datetime.now(timezone.utc))
    for proposal in waiting:
        cost = proposal.qty * proposal.entry
        risk = proposal.qty * (proposal.entry - proposal.stop)
        st.markdown(
            f'<div class="ts-brief" style="border-right-color:{PALETTE["accent"]}">'
            f'<div class="ts-brief-title" style="color:{PALETTE["accent"]}">✋ הרובוט מבקש את אישורך</div>'
            f'<div class="ts-brief-headline">לקנות {proposal.qty} מניות של {esc(proposal.symbol)} '
            f'בסביבות {money(proposal.entry)}</div>'
            f'<ul><li>עלות משוערת: {money(cost)}</li>'
            f'<li>הגנה: מכירה אוטומטית אם תרד ל-{money(proposal.stop)} '
            f'(הפסד מרבי של כ-{money(risk)})</li>'
            f'<li>הבקשה תקפה עד סוף יום המסחר. באישור, שומר הסיכון בודק שוב במחיר של אותו רגע.</li>'
            f"</ul></div>",
            unsafe_allow_html=True,
        )
        yes_col, no_col = st.columns(2)
        if yes_col.button("✅ מאשר", type="primary", disabled=not armed,
                          width="stretch", key=f"approve_{proposal.proposal_id}"):
            with st.spinner("שומר הסיכון בודק ושולח…"):
                outcome = session.execute_proposal(proposal.proposal_id)
            st.session_state[f"proposal_note_{client_id}"] = outcome
            invalidate()
            st.rerun()
        if no_col.button("לא עכשיו", disabled=not armed, width="stretch",
                         key=f"decline_{proposal.proposal_id}"):
            session.decline_proposal(proposal.proposal_id)
            invalidate()
            st.rerun()
    if waiting:
        locked_hint(armed)
    note = st.session_state.pop(f"proposal_note_{client_id}", None)
    if note:
        st.info(note)


def autopilot_status(session) -> None:
    from tradesys.agents.risk_manager import AUTO_PILOT_RISK_PCT

    regime = session.market_regime()
    rising = regime is not None and regime.risk_on
    state = "בעלייה" if rising else "חלש" if regime is not None else "לא ידוע"
    colour = PALETTE["positive"] if rising else PALETTE["warning"]
    detail = (
        f"S&amp;P 500 ב-{regime.price:,.2f} מול ממוצע 200 הימים {regime.average:,.2f}"
        if regime is not None else "אין כרגע נתוני שוק, ולכן הרובוט נוהג בזהירות"
    )
    st.markdown(
        f'<div class="ts-vault"><div class="ts-vault-label">הטייס האוטומטי פעיל</div>'
        f'<div style="font-size:18px;font-weight:700">השוק {state} · '
        f'<span style="color:{colour}">{AUTO_PILOT_RISK_PCT[rising]}% סיכון לעסקה</span></div>'
        f'<div class="ts-agent-text" style="margin-top:6px">{detail}</div></div>',
        unsafe_allow_html=True,
    )


def tab_protection(client_id: str, snapshot: dict, session, armed: bool) -> None:
    """שליטה וסיכון: טייס אוטומטי או הגדרות הלקוח, הקצאה, מד סיכון ורצפה."""
    if control_panel(client_id, snapshot, session, armed) == "auto":
        autopilot_status(session)
    else:
        risk_meter(client_id, snapshot, session, armed)
    floor_status(snapshot)


def risk_meter(client_id: str, snapshot: dict, session, armed: bool) -> None:
    """מד סיכון: בחירת רמת הסיכון, ומצב התיק מול המגבלות שלה."""
    from tradesys.clients.profile import PROTECTED_RISK_LEVEL, RISK_LEVELS
    from tradesys.clients.tracks import effective_level, read_track, write_track

    st.markdown("##### מד סיכון")
    st.caption(
        "רמת הסיכון קובעת יחד את כל מגבלות ההפסד: רצפת הקרן, הסיכון בכל עסקה, "
        "ההפסד היומי המרבי והעצירה בירידה מהשיא. רמות 1–3 נמצאות בתוך שמירת הקרן."
    )

    choice = read_track(session.tenant.data_dir)
    active = effective_level(session.profile, choice)
    equity = snapshot["equity"]
    drawdown = snapshot["drawdown_pct"]
    limit = snapshot["max_drawdown_pct"]

    gauges = st.columns(2)
    green, amber, red = "rgba(38,208,124,.35)", "rgba(245,165,36,.35)", "rgba(242,84,91,.4)"
    with gauges[0]:
        st.plotly_chart(
            risk_gauge(
                active.level if active else 0,
                len(RISK_LEVELS),
                f"רמת סיכון · {active.name}" if active else "רמת סיכון · ברירת מחדל",
                [(0, green), (PROTECTED_RISK_LEVEL, red)],
            ),
            width="stretch",
            config={"displayModeBar": False},
        )
    with gauges[1]:
        st.plotly_chart(
            risk_gauge(
                round(drawdown, 2),
                limit,
                f"ירידה מהשיא מול המגבלה ({limit}%)",
                [(0, green), (limit * 0.5, amber), (limit * 0.8, red)],
            ),
            width="stretch",
            config={"displayModeBar": False},
        )

    max_level = session.profile.max_risk_level
    selected = st.select_slider(
        "בחר רמת סיכון",
        options=list(RISK_LEVELS),
        value=active.level if active else 2,
        format_func=lambda lvl: f"{lvl} · {RISK_LEVELS[lvl].name}",
        key=f"risk_pick_{client_id}",
    )
    target = RISK_LEVELS[selected]
    current = session.profile.risk
    st.dataframe(
        pd.DataFrame(
            {
                "מגבלה": [
                    "רצפת קרן",
                    "סיכון מרבי בעסקה",
                    "הפסד יומי מרבי",
                    "עצירה בירידה מהשיא",
                ],
                "עכשיו": [
                    f"{current.capital_floor_pct:g}%" if current.capital_floor_pct else "—",
                    f"{current.risk_per_trade_pct:g}%",
                    f"{current.max_daily_loss_pct:g}%",
                    f"{current.max_drawdown_pct:g}%",
                ],
                f"ברמה {selected}": [
                    f"{target.capital_floor_pct:g}% ({money(equity * target.capital_floor_pct / 100)})",
                    f"{target.risk_per_trade_pct:g}% ({money(equity * target.risk_per_trade_pct / 100)})",
                    f"{target.max_daily_loss_pct:g}% ({money(equity * target.max_daily_loss_pct / 100)})",
                    f"{target.max_drawdown_pct:g}%",
                ],
            }
        ),
        width="stretch",
        hide_index=True,
    )

    permitted = selected <= max_level
    acknowledged = False
    if not permitted:
        st.info(
            f"רמה {selected} דורשת אישור של מנהל המערכת. הרמה המרבית שאושרה לחשבון "
            f"זה היא {max_level}.",
            icon="🔒",
        )
    elif target.beyond_protection:
        worst = 100 - target.capital_floor_pct
        st.error(
            f"רמה זו חורגת משמירת הקרן. התיק עלול לרדת עד {worst:g}% "
            f"(כ-{money(equity * worst / 100)}) לפני שהמסחר ייעצר, ופער מחיר (גאפ) "
            "אחרי דוח או חדשות עלול להגדיל את ההפסד גם מעבר לכך.",
            icon="⚠️",
        )
        acknowledged = st.checkbox(
            f"קראתי והבנתי: ברמה זו אני עלול/ה להפסיד {worst:g}% מהקרן ויותר, "
            "והבחירה היא על אחריותי.",
            key=f"risk_ack_{client_id}_{selected}",
        )
    if permitted and drawdown >= target.max_drawdown_pct:
        st.warning(
            f"הירידה הנוכחית ({drawdown:.2f}%) כבר גבוהה ממגבלת הרמה הזו "
            f"({target.max_drawdown_pct:g}%). בחירה בה תעצור את המסחר מיד.",
            icon="🛑",
        )

    unchanged = active is not None and selected == active.level
    locked_hint(armed)
    if unchanged:
        st.caption("זו הרמה שכבר פעילה.")
    if st.button(
        "החל רמת סיכון",
        type="primary",
        disabled=(
            not armed
            or not permitted
            or unchanged
            or (target.beyond_protection and not acknowledged)
        ),
        width="stretch",
    ):
        write_track(
            session.tenant.data_dir,
            selected,
            equity,
            acknowledged=acknowledged,
            max_level=max_level,
        )
        invalidate()
        st.rerun()

    if choice is not None and st.button("חזרה להגדרות ברירת המחדל", disabled=not armed):
        write_track(session.tenant.data_dir, None)
        invalidate()
        st.rerun()


def floor_status(snapshot: dict) -> None:
    floor = snapshot["floor"]
    st.markdown("")
    if floor is None:
        st.info("לא נבחרה רמת סיכון. המערכת פועלת לפי מגבלות ברירת המחדל של החשבון.")
    else:
        metrics = st.columns(4)
        metrics[0].metric("רצפה", money(floor["floor"]))
        metrics[1].metric("כרית מעל הרצפה", money(floor["cushion"]))
        metrics[2].metric("בסיכון בפוזיציות פתוחות", money(floor["open_risk"]))
        metrics[3].metric("אם כל הסטופים יופעלו עכשיו", money(floor["worst_case"]))
        st.caption(
            f"הרצפה מחושבת משווי התיק ביום בחירת הרמה: {money(floor['base'])}."
        )
        if floor["available"] <= 0:
            st.warning("התיק ברצפה. עסקאות חדשות לא ייפתחו עד שהכרית תתאושש.", icon="🛡")

    st.caption(
        "חשוב: זו רצפת יעד ולא הבטחה. אם מחיר נפתח בפער (גאפ) מעבר לסטופ, "
        "למשל אחרי דוח או חדשות, ההפסד בפועל יכול לעבור את הרצפה."
    )


def tab_controls(
    client_id: str, snapshot: dict, session, armed: bool, demo: bool = False
) -> None:
    if demo:
        st.caption(
            "במצב הדגמה אפשר להריץ מחזורי מסחר על חשבון נייר. "
            "מסחר אוטומטי ועצירות חירום שמורים לחשבונות רשומים."
        )
    elif not armed:
        st.info("הפעולות נעולות. הפעל **אפשר פעולות** בסרגל הצד כדי לבצע שינויים.")

    suspended, suspend_reason = tenant_suspended(session)
    if suspended:
        st.error(
            f"החשבון מושהה — {suspend_reason}. לא ניתן לפתוח פוזיציות חדשות.", icon="⛔"
        )

    st.markdown("##### הפעלת המערכת")
    left, right = st.columns(2)

    with left:
        st.caption("מחזור מלא: בדיקת פוזיציות קיימות, ואז סריקה, אישור סיכונים וביצוע.")
        if st.button(
            "הרץ מחזור מסחר",
            disabled=not armed or suspended,
            width="stretch",
            type="primary",
        ):
            try:
                with st.spinner("מריץ מחזור…"):
                    report = session.run_cycle()
                st.success(report.summary())
                for symbol, reasons in report.rejections:
                    st.caption(f"{symbol} נדחה: {'; '.join(reasons)}")
                invalidate()
            except TradesysError as exc:
                st.error(f"המחזור נכשל: {exc}")
            except Exception:
                st.error("אירעה שגיאה בלתי צפויה.")
                st.code(traceback.format_exc())

    with right:
        st.caption("בדיקת סטופים ויעדים בלבד. לעולם אינה פותחת פוזיציה חדשה.")
        if st.button("בדוק פוזיציות פתוחות", disabled=not armed, width="stretch"):
            try:
                with st.spinner("בודק…"):
                    report = session.monitor.check_once()
                st.success(report.summary())
                for closed in report.closed:
                    st.caption(
                        f"{closed.trade.symbol}: {closed.reason.value} "
                        f"במחיר {closed.exit_price:.2f}"
                    )
                invalidate()
            except TradesysError as exc:
                st.error(str(exc))

    st.divider()
    if demo:
        upgrade_cta(
            "מסחר אוטומטי וניהול סיכונים מתמשך",
            "בחשבון רשום המערכת מריצה מחזורי מסחר ברקע, מנטרת סטופים ויעדים "
            "באופן רציף, ולומדת מכל עסקה שנסגרת.",
            key="cta_controls",
        )
        return

    st.markdown("##### מסחר אוטומטי")
    running = automation_enabled(session)
    if not snapshot["paper"]:
        st.error("מסחר אוטומטי אינו זמין בחשבון אמיתי דרך מסך זה.", icon="⛔")
    elif running:
        st.success(f"פעיל — מחזור כל {AUTOMATION_INTERVAL_SECONDS} שניות.")
        if st.button("עצור מסחר אוטומטי", disabled=not armed, width="stretch"):
            set_automation(session, False)
            st.rerun()
    else:
        st.caption("מריץ מחזור מסחר במרווחים קבועים כל עוד הדף פתוח.")
        confirm = st.text_input(f"הקלד `{client_id}` לאישור", key="auto_confirm")
        if st.button(
            "הפעל מסחר אוטומטי",
            disabled=not armed or suspended or confirm != client_id,
            width="stretch",
        ):
            set_automation(session, True)
            st.rerun()

    st.divider()
    st.markdown("##### עצירת חירום")
    stop_col, close_col = st.columns(2)
    with stop_col:
        st.caption("עצירה חוסמת פתיחת פוזיציות חדשות. סגירת פוזיציות ממשיכה לעבוד.")
        if st.button(
            "עצור הכול",
            disabled=not armed or snapshot["halted"],
            width="stretch",
            type="primary",
        ):
            session.journal.halt("עצירה יזומה על ידי הלקוח")
            set_automation(session, False)
            invalidate()
            st.rerun()

    with close_col:
        open_df = snapshot["open_df"]
        if open_df.empty:
            st.caption("אין פוזיציות לסגירה.")
        else:
            symbol = st.selectbox("סגור פוזיציה", open_df["סימבול"].tolist())
            if st.button(f"סגור {symbol}", disabled=not armed, width="stretch"):
                try:
                    report = session.monitor.close_manually(symbol, "סגירה יזומה")
                    st.success(report.summary())
                    invalidate()
                    st.rerun()
                except TradesysError as exc:
                    st.error(str(exc))

    with st.expander("פרופיל הסיכון שלי"):
        st.json(snapshot["risk"])


# ------------------------------------------------------------------ אוטומציה

@st.fragment(run_every=AUTOMATION_INTERVAL_SECONDS)
def automation_tick(client_id: str) -> None:
    session = get_session(client_id)
    if not automation_enabled(session) or not session.profile.broker.paper:
        return
    suspended, _ = tenant_suspended(session)
    if suspended:
        return
    try:
        report = session.run_cycle()
        st.caption(
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} — {report.summary()}"
        )
    except Exception as exc:
        st.warning(f"מחזור אוטומטי נכשל: {exc}")


# ---------------------------------------------------------------- פורטל לקוח

def client_portal(client_id: str, demo: bool = False) -> None:
    try:
        session = get_session(client_id)
        snapshot = load_snapshot(client_id, st.session_state.get("_bust", 0))
    except TradesysError as exc:
        st.error(f"לא ניתן לטעון את פרופיל הלקוח: {exc}")
        if st.button("התנתק"):
            logout()
        return

    with st.sidebar:
        st.markdown("## ◧ tradesys")
        st.caption(f"**{snapshot['display_name']}**")
        st.markdown(
            f'<span class="ts-pill" style="background:rgba(76,196,245,.15);'
            f'color:{PALETTE["accent"]}">{esc(client_id)}</span>',
            unsafe_allow_html=True,
        )
        st.divider()
        st.metric("שווי תיק", money(snapshot["equity"]))
        st.metric("פוזיציות פתוחות", snapshot["position_count"])
        st.divider()
        if demo:
            # הדגמה רצה תמיד על ברוקר נייר, ולכן אין צורך בנעילה ידנית
            armed = True
            st.info("מצב הדגמה — חשבון נייר בלבד.", icon="▶")
        else:
            armed = st.toggle(
                "אפשר פעולות",
                value=False,
                key="armed",
                help="פעולות משנות-מצב נעולות עד להפעלה, ומתאפסות בכל טעינה מחדש.",
            )
            if armed:
                st.warning("הפעולות פעילות.", icon="⚠")
        if st.button("רענן נתונים", width="stretch"):
            invalidate()
            st.rerun()
        st.divider()
        if demo:
            if st.button("פתח חשבון אמיתי", width="stretch", type="primary"):
                st.session_state["role"] = None
                st.session_state["_goto_register"] = True
                st.rerun()
            if st.button("צא ממצב הדגמה", width="stretch"):
                logout()
        elif st.button("התנתק", width="stretch"):
            logout()
        st.caption(f"עודכן {snapshot['taken_at'].strftime('%H:%M:%S')} UTC")

    st.markdown(
        f'<div class="ts-hero"><div class="ts-hero-title">'
        f'{esc(snapshot["display_name"])}</div>'
        f'<div class="ts-hero-sub">{len(snapshot["symbols"])} ניירות במעקב · '
        f'{ltr(", ".join(snapshot["symbols"]))}</div></div>',
        unsafe_allow_html=True,
    )

    if demo:
        st.markdown(
            '<div class="ts-demo-bar">▶ מצב הדגמה · '
            "כל המספרים במסך זה הם תוצאות סימולציה שהופקו על ידי מנוע מסחר בחשבון "
            f"נייר על {price_feed_label()}. לא נשלחו פקודות אמיתיות ולא הועמד הון בסיכון. "
            "לתוצאות סימולציה יש מגבלות מובנות והן אינן מייצגות מסחר בפועל ואינן "
            "מעידות על תוצאות עתידיות.</div>",
            unsafe_allow_html=True,
        )

    if snapshot["paper"]:
        st.markdown(
            '<div class="ts-banner ts-banner-ok">◇ מצב סימולציה — המסחר מתבצע '
            "בחשבון נייר. אין חשיפה לכסף אמיתי.</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="ts-banner ts-banner-live">● חשבון אמיתי — פעולות במסך זה '
            "משפיעות על כסף אמיתי.</div>",
            unsafe_allow_html=True,
        )

    suspended, suspend_reason = tenant_suspended(session)
    if suspended:
        st.markdown(
            f'<div class="ts-banner ts-banner-live">✕ החשבון מושהה — '
            f'{esc(suspend_reason or "ללא פירוט")}. סגירת פוזיציות עדיין אפשרית.</div>',
            unsafe_allow_html=True,
        )
    if snapshot["halted"]:
        st.markdown(
            f'<div class="ts-banner ts-banner-warn">■ המסחר נעצר — '
            f'{esc(snapshot["halt_reason"])}</div>',
            unsafe_allow_html=True,
        )

    proposals_banner(client_id, session, armed)

    tabs = st.tabs(
        [
            "הבית",
            "שליטה וסיכון",
            "גרפים",
            "פעילות הסוכנים",
            "חיבור ברוקר",
            "חיוב ותשלומים",
            "בקרה",
        ],
        # Keyed so a rerun (any slider or dropdown) keeps the open tab instead
        # of throwing the user back to the first one.
        key=f"client_tabs_{client_id}",
        on_change="rerun",
    )
    with tabs[0]:
        tab_home(client_id, snapshot, session)
    with tabs[1]:
        tab_protection(client_id, snapshot, session, armed)
    with tabs[2]:
        tab_charts(client_id, snapshot, session)
    with tabs[3]:
        tab_agents(snapshot, session, armed)
    with tabs[4]:
        tab_broker(client_id, demo)
    with tabs[5]:
        tab_billing(client_id, snapshot, demo)
    with tabs[6]:
        tab_controls(client_id, snapshot, session, armed, demo)

    if (
        not scheduler_mode()
        and not demo
        and automation_enabled(session)
        and snapshot["paper"]
        and not suspended
    ):
        automation_tick(client_id)


def logout() -> None:
    for key in ("role", "auth_client", "_admin_unlocked", "armed"):
        st.session_state.pop(key, None)
    st.rerun()


# ---------------------------------------------------------------------- ראשי

def main() -> None:
    role = st.session_state.get("role")

    if role == "admin":
        from admin_panel import render_admin

        render_admin()
        return

    if role == "demo":
        client_portal(DEMO_CLIENT_ID, demo=True)
        return

    if role == "client":
        client_id = st.session_state.get("auth_client", "")
        account = get_account_store().get(client_id)
        # נבדק בכל טעינה: שלילת גישה על ידי המנהל מנתקת מיד, לא רק בכניסה הבאה
        if account is None or not account.active:
            st.error("הגישה לחשבון בוטלה. פנה למנהל המערכת.")
            if st.button("חזרה למסך הכניסה"):
                logout()
            return
        client_portal(client_id)
        return

    login_screen()


if __name__ == "__main__":
    main()
