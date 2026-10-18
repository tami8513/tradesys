"""Small window for saving the Alpaca keys into .env without a terminal.

A console getpass prompt turned out to be easy to break: Ctrl+C in a console
aborts the program instead of copying. This window uses PASTE buttons that read
the clipboard directly, masks the values, shows only their length, and catches
the common mistake of pasting the Key and Secret into each other's slot.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

sys.path.insert(0, str(Path(__file__).resolve().parent))
from set_secret import ENV_PATH, set_secret  # noqa: E402

KEY_NAME = "TRADESYS_MARKET_DATA__KEY_ID"
SECRET_NAME = "TRADESYS_MARKET_DATA__SECRET_KEY"


def problems(key: str, secret: str) -> list[str]:
    found = []
    if not key or not secret:
        found.append("Both the Key and the Secret are needed.")
        return found
    if len(key) > len(secret):
        found.append("These look swapped: the Key is the SHORTER code, the Secret is the longer one.")
    elif not key.startswith(("PK", "AK")):
        found.append("The Key usually starts with PK. Check you copied the Key box.")
    if any(ch.isspace() for ch in key + secret):
        found.append("A code contains a space. Copy it again with Ctrl+A, Ctrl+C.")
    return found


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.values = {KEY_NAME: "", SECRET_NAME: ""}
        root.title("tradesys - save Alpaca keys")
        root.geometry("520x330")
        root.resizable(False, False)

        tk.Label(
            root,
            text="In Alpaca: click inside a box, press Ctrl+A then Ctrl+C.\n"
            "Then click the matching PASTE button below.",
            font=("Segoe UI", 11),
            justify="left",
        ).pack(pady=(16, 10))

        self.status = {}
        for name, label in ((KEY_NAME, "1.  Key"), (SECRET_NAME, "2.  Secret")):
            row = tk.Frame(root)
            row.pack(fill="x", padx=24, pady=8)
            tk.Label(row, text=label, width=10, anchor="w", font=("Segoe UI", 12, "bold")).pack(
                side="left"
            )
            tk.Button(
                row,
                text="PASTE",
                width=10,
                font=("Segoe UI", 11),
                command=lambda n=name: self.paste(n),
            ).pack(side="left", padx=8)
            status = tk.Label(row, text="not pasted yet", fg="#888", font=("Segoe UI", 10))
            status.pack(side="left", padx=8)
            self.status[name] = status

        tk.Button(
            root,
            text="SAVE",
            width=20,
            font=("Segoe UI", 12, "bold"),
            bg="#2e7d32",
            fg="white",
            command=self.save,
        ).pack(pady=(22, 6))
        tk.Label(
            root,
            text="The codes are never shown on screen.",
            fg="#888",
            font=("Segoe UI", 9),
        ).pack()

    def paste(self, name: str) -> None:
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            text = ""
        if not text:
            messagebox.showwarning("Nothing copied", "The clipboard is empty. Copy the code in Alpaca first.")
            return
        self.values[name] = text
        self.status[name].config(text=f"OK - {len(text)} characters", fg="#2e7d32")

    def save(self) -> None:
        key, secret = self.values[KEY_NAME], self.values[SECRET_NAME]
        issues = problems(key, secret)
        if issues:
            messagebox.showwarning("Check the codes", "\n\n".join(issues))
            return
        set_secret(KEY_NAME, key)
        set_secret(SECRET_NAME, secret)
        messagebox.showinfo(
            "Saved",
            f"Both keys were saved to {ENV_PATH.name}.\n\n"
            "You can close this window and tell Claude you finished.",
        )
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
