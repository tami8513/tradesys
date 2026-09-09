"""עדכון בטוח של ערך יחיד בקובץ .env, בלי לפתוח עורך טקסט.

למה זה קיים: עריכה ידנית ב-Notepad/עורכים עם כמה טאבים פתוחים היא מקור נפוץ
לטעויות — שמירה בטאב הלא נכון, שורה חדשה מיותרת, רווח נוסף, קידוד לא תואם.
הסקריפט הזה קורא את הערך מהטרמינל (ללא הצגה על המסך), ומעדכן רק את השורה
המבוקשת בקובץ .env שנמצא תמיד לצד הסקריפט הזה — בלי אפשרות לטעות בנתיב.

שימוש:
    python scripts/set_secret.py TRADESYS_FINNHUB_API_KEY
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def set_secret(key: str, value: str) -> None:
    if not key or not key.replace("_", "").isalnum():
        raise ValueError(f"invalid environment variable name: {key!r}")

    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    prefix = f"{key}="
    new_line = f"{key}={value}"
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix) or line.strip() == f"# {prefix}":
            lines[i] = new_line
            replaced = True
            break

    if not replaced:
        lines.append(new_line)

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/set_secret.py <ENV_VAR_NAME>")
        return 1

    key = sys.argv[1]
    print(f"קובץ יעד: {ENV_PATH}")
    print(f"מזין ערך עבור {key} (ההקלדה לא תוצג על המסך).")
    value = getpass.getpass("הדבק/הקלד את הערך ולחץ Enter: ").strip()

    if not value:
        print("לא הוזן ערך — לא בוצע שינוי.")
        return 1

    set_secret(key, value)
    print(f"נשמר. {key} עודכן ב-{ENV_PATH.name} (האורך שנשמר: {len(value)} תווים).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
