"""Copy saved keys from .env to the clipboard, one button per key.

For pasting into a hosting dashboard (Render) without opening .env in a text
editor and without the values ever appearing on screen.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from set_secret import ENV_PATH  # noqa: E402

NAMES = (
    "TRADESYS_MARKET_DATA__KEY_ID",
    "TRADESYS_MARKET_DATA__SECRET_KEY",
    "TRADESYS_FINNHUB_API_KEY",
    "TRADESYS_ADMIN_PASSCODE",
    "TRADESYS_VAULT_KEY",
)


def saved_values() -> dict[str, str]:
    from dotenv import dotenv_values

    values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    return {name: values[name] for name in NAMES if values.get(name)}


def main() -> None:
    root = tk.Tk()
    root.title("tradesys - copy keys")
    values = saved_values()

    tk.Label(
        root,
        text="Click COPY, then paste (Ctrl+V) into the field with the SAME name in Render.",
        font=("Segoe UI", 11),
    ).pack(padx=16, pady=(14, 8))

    if not values:
        tk.Label(root, text="No saved keys found in .env", fg="#c62828").pack(pady=12)

    for name, value in values.items():
        row = tk.Frame(root)
        row.pack(fill="x", padx=16, pady=5)
        tk.Label(row, text=name, width=36, anchor="w", font=("Consolas", 10)).pack(side="left")
        status = tk.Label(row, text="", fg="#2e7d32", width=10, font=("Segoe UI", 10))

        def copy(v: str = value, s: tk.Label = status) -> None:
            root.clipboard_clear()
            root.clipboard_append(v)
            root.update()
            s.config(text="copied")

        tk.Button(row, text="COPY", width=8, command=copy).pack(side="left", padx=6)
        status.pack(side="left")

    tk.Label(root, text="Keep this window open until you finish pasting.", fg="#888").pack(
        pady=(10, 14)
    )
    root.mainloop()


if __name__ == "__main__":
    main()
