"""Platform-level accounts, broker credential vault and billing.

Three concerns that sit *above* the tenant boundary but never cross it:

  * **Accounts** — who may sign in, and as which tenant. Passwords are stored as
    PBKDF2-HMAC-SHA256 digests with a per-account salt. Plaintext passwords are
    never written to disk and never logged.

  * **Vault** — broker API keys. Encrypted at rest with Fernet, keyed from
    TRADESYS_VAULT_KEY. If that variable is absent the vault refuses to store
    anything rather than falling back to plaintext. A B2B platform holding other
    people's brokerage keys has no business degrading quietly.

  * **Billing** — subscription state and performance fees, with a high-water mark
    so the same gains are never charged twice after a drawdown recovery.

Platform state lives in var/_platform. A tenant's client_id must match
^[a-z][a-z0-9_]{2,39}$ (see core.identity), so a leading underscore can never
collide with a real tenant directory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradesys.clients.profile import ClientProfile
from tradesys.clients.secrets import BrokerCredentials, EnvSecretResolver
from tradesys.core.errors import CredentialError
from tradesys.core.identity import validate_client_id

PBKDF2_ITERATIONS = 600_000
PLATFORM_DIRNAME = "_platform"


# ------------------------------------------------------------------ passwords

def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Return (salt_hex, digest_hex). Never store or log the password itself."""
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(candidate.hex(), digest_hex)


# ------------------------------------------------------------------- accounts

@dataclass(frozen=True, slots=True)
class Account:
    client_id: str
    salt: str
    digest: str
    active: bool = True
    display_name: str = ""
    created_at: str = ""
    last_login: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "salt": self.salt,
            "digest": self.digest,
            "active": self.active,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "last_login": self.last_login,
        }


class AccountStore:
    """File-backed account directory for the client portal."""

    def __init__(self, data_root: Path) -> None:
        self._dir = Path(data_root) / PLATFORM_DIRNAME
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "accounts.json"

    def _load(self) -> dict[str, Account]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        return {
            key: Account(
                client_id=value["client_id"],
                salt=value["salt"],
                digest=value["digest"],
                active=bool(value.get("active", True)),
                display_name=value.get("display_name", ""),
                created_at=value.get("created_at", ""),
                last_login=value.get("last_login", ""),
            )
            for key, value in raw.items()
        }

    def _save(self, accounts: dict[str, Account]) -> None:
        payload = {key: account.to_json() for key, account in accounts.items()}
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self._path)
        _restrict(self._path)

    def list_accounts(self) -> list[Account]:
        return sorted(self._load().values(), key=lambda a: a.client_id)

    def get(self, client_id: str) -> Account | None:
        return self._load().get(client_id)

    def create(self, client_id: str, password: str, display_name: str = "") -> Account:
        validate_client_id(client_id)
        accounts = self._load()
        if client_id in accounts:
            raise ValueError(f"account already exists for {client_id}")
        salt, digest = hash_password(password)
        account = Account(
            client_id=client_id,
            salt=salt,
            digest=digest,
            active=True,
            display_name=display_name,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        accounts[client_id] = account
        self._save(accounts)
        return account

    def set_password(self, client_id: str, password: str) -> None:
        accounts = self._load()
        account = accounts.get(client_id)
        if account is None:
            raise ValueError(f"no account for {client_id}")
        salt, digest = hash_password(password)
        accounts[client_id] = Account(
            client_id=account.client_id,
            salt=salt,
            digest=digest,
            active=account.active,
            display_name=account.display_name,
            created_at=account.created_at,
            last_login=account.last_login,
        )
        self._save(accounts)

    def set_active(self, client_id: str, active: bool) -> None:
        accounts = self._load()
        account = accounts.get(client_id)
        if account is None:
            raise ValueError(f"no account for {client_id}")
        accounts[client_id] = Account(
            client_id=account.client_id,
            salt=account.salt,
            digest=account.digest,
            active=active,
            display_name=account.display_name,
            created_at=account.created_at,
            last_login=account.last_login,
        )
        self._save(accounts)

    def delete(self, client_id: str) -> bool:
        accounts = self._load()
        if client_id not in accounts:
            return False
        accounts.pop(client_id)
        self._save(accounts)
        return True

    def authenticate(self, client_id: str, password: str) -> Account | None:
        """Return the account on success, None on any failure.

        Failure is deliberately indistinguishable between 'no such account' and
        'wrong password', and a dummy verification runs for unknown accounts so the
        response time does not reveal which client ids exist.
        """
        account = self._load().get(client_id)
        if account is None:
            hash_password("dummy-password-for-timing")
            return None
        if not verify_password(password, account.salt, account.digest):
            return None
        if not account.active:
            return None
        self._touch_login(client_id)
        return account

    def _touch_login(self, client_id: str) -> None:
        accounts = self._load()
        account = accounts.get(client_id)
        if account is None:
            return
        accounts[client_id] = Account(
            client_id=account.client_id,
            salt=account.salt,
            digest=account.digest,
            active=account.active,
            display_name=account.display_name,
            created_at=account.created_at,
            last_login=datetime.now(timezone.utc).isoformat(),
        )
        self._save(accounts)


def _restrict(path: Path) -> None:
    """Best-effort owner-only permissions. No-op where the OS does not support it."""
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


# ---------------------------------------------------------------------- vault

class VaultLocked(Exception):
    """TRADESYS_VAULT_KEY is not set, so credentials cannot be read or written."""


def _fernet():
    """Build the Fernet cipher from TRADESYS_VAULT_KEY.

    Raises VaultLocked rather than falling back to plaintext. Storing a client's
    live brokerage keys unencrypted is not an acceptable degraded mode.
    """
    key_material = os.getenv("TRADESYS_VAULT_KEY", "")
    if not key_material:
        raise VaultLocked(
            "TRADESYS_VAULT_KEY is not set — the broker vault is locked. "
            "Set it before connecting a broker."
        )
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise VaultLocked(
            "the 'cryptography' package is required for the broker vault "
            '(pip install -e ".[dashboard]")'
        ) from exc

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        key_material.encode("utf-8"),
        b"tradesys-broker-vault-v1",
        PBKDF2_ITERATIONS,
    )
    return Fernet(base64.urlsafe_b64encode(derived))


@dataclass(frozen=True, slots=True)
class VaultEntry:
    broker: str
    key_id_masked: str
    paper: bool
    verified_at: str
    account_label: str = ""


class BrokerVault:
    """Per-tenant encrypted store for broker API credentials.

    Secrets are write-only from the UI's perspective: once saved, only a masked
    key id is ever returned for display. There is no read path that hands a
    plaintext secret back to a template.
    """

    def __init__(self, tenant_dir: Path) -> None:
        self._path = Path(tenant_dir) / "broker_credentials.enc"
        self._meta_path = Path(tenant_dir) / "broker_credentials.meta.json"

    @property
    def connected(self) -> bool:
        return self._path.exists() and self._meta_path.exists()

    def metadata(self) -> VaultEntry | None:
        """Non-secret description of the stored connection, safe to render."""
        if not self._meta_path.exists():
            return None
        try:
            raw = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        return VaultEntry(
            broker=raw.get("broker", "alpaca"),
            key_id_masked=raw.get("key_id_masked", ""),
            paper=bool(raw.get("paper", True)),
            verified_at=raw.get("verified_at", ""),
            account_label=raw.get("account_label", ""),
        )

    def store(
        self,
        *,
        broker: str,
        key_id: str,
        secret_key: str,
        paper: bool,
        account_label: str = "",
    ) -> VaultEntry:
        cipher = _fernet()
        payload = json.dumps(
            {"broker": broker, "key_id": key_id, "secret_key": secret_key, "paper": paper}
        ).encode("utf-8")
        self._path.write_bytes(cipher.encrypt(payload))
        _restrict(self._path)

        entry = VaultEntry(
            broker=broker,
            key_id_masked=mask(key_id),
            paper=paper,
            verified_at=datetime.now(timezone.utc).isoformat(),
            account_label=account_label,
        )
        self._meta_path.write_text(
            json.dumps(
                {
                    "broker": entry.broker,
                    "key_id_masked": entry.key_id_masked,
                    "paper": entry.paper,
                    "verified_at": entry.verified_at,
                    "account_label": entry.account_label,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return entry

    def load(self) -> dict[str, Any] | None:
        """Decrypt. Only the SecretResolver should call this — never a UI template."""
        if not self._path.exists():
            return None
        cipher = _fernet()
        try:
            return json.loads(cipher.decrypt(self._path.read_bytes()).decode("utf-8"))
        except Exception as exc:
            raise CredentialError(
                "stored broker credentials could not be decrypted — "
                "TRADESYS_VAULT_KEY may have changed"
            ) from exc

    def clear(self) -> None:
        for path in (self._path, self._meta_path):
            if path.exists():
                path.unlink()


def mask(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


def verify_alpaca_credentials(
    key_id: str, secret_key: str, paper: bool, timeout: float = 10.0
) -> tuple[bool, str]:
    """Check credentials against Alpaca before storing them.

    Returns (ok, message). Verifying first is what makes the wizard forgiving: a
    typo fails immediately with a readable reason instead of surfacing days later
    as a silent execution failure.
    """
    try:
        import httpx
    except ImportError:
        return False, "החבילה httpx חסרה — התקן עם pip install -e \".[dashboard]\""

    base = (
        "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
    )
    try:
        response = httpx.get(
            f"{base}/v2/account",
            headers={
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret_key,
            },
            timeout=timeout,
        )
    except Exception as exc:
        return False, f"לא ניתן להתחבר לברוקר: {exc}"

    if response.status_code == 403 or response.status_code == 401:
        return False, "המפתחות נדחו על ידי הברוקר. בדוק שהעתקת את שניהם במלואם."
    if response.status_code >= 400:
        return False, f"הברוקר החזיר שגיאה {response.status_code}."
    try:
        payload = response.json()
    except ValueError:
        return False, "תשובה לא צפויה מהברוקר."
    return True, payload.get("account_number", "")


class VaultSecretResolver(EnvSecretResolver):
    """SecretResolver that prefers the encrypted vault, falling back to env vars.

    Implements the same interface the engine already expects, so connecting a
    broker through the UI needs no change anywhere in the agent stack.
    """

    def __init__(self, data_root: Path, env: dict[str, str] | None = None) -> None:
        super().__init__(env)
        self._data_root = Path(data_root)

    def broker_credentials(self, profile: ClientProfile) -> BrokerCredentials:
        vault = BrokerVault(self._data_root / profile.client_id)
        if vault.connected:
            stored = vault.load()
            if stored:
                paper = bool(stored.get("paper", True))
                return BrokerCredentials(
                    key_id=stored["key_id"],
                    secret_key=stored["secret_key"],
                    base_url=(
                        "https://paper-api.alpaca.markets"
                        if paper
                        else "https://api.alpaca.markets"
                    ),
                    paper=paper,
                )
        return super().broker_credentials(profile)


# -------------------------------------------------------------------- billing

@dataclass(frozen=True, slots=True)
class Billing:
    model: str = "subscription"          # subscription | profit_share
    subscription_active: bool = False
    monthly_price: float = 499.0
    currency: str = "₪"
    profit_share_pct: float = 20.0
    high_water_mark: float = 0.0
    fees_settled: float = 0.0
    renews_on: str = ""
    history: list = field(default_factory=list)


def billing_path(tenant_dir: Path) -> Path:
    return Path(tenant_dir) / "billing.json"


def read_billing(tenant_dir: Path) -> Billing:
    path = billing_path(tenant_dir)
    if not path.exists():
        return Billing()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return Billing()
    return Billing(
        model=raw.get("model", "subscription"),
        subscription_active=bool(raw.get("subscription_active", False)),
        monthly_price=float(raw.get("monthly_price", 499.0)),
        currency=raw.get("currency", "₪"),
        profit_share_pct=float(raw.get("profit_share_pct", 20.0)),
        high_water_mark=float(raw.get("high_water_mark", 0.0)),
        fees_settled=float(raw.get("fees_settled", 0.0)),
        renews_on=raw.get("renews_on", ""),
        history=list(raw.get("history", [])),
    )


def write_billing(tenant_dir: Path, billing: Billing) -> None:
    billing_path(tenant_dir).write_text(
        json.dumps(
            {
                "model": billing.model,
                "subscription_active": billing.subscription_active,
                "monthly_price": billing.monthly_price,
                "currency": billing.currency,
                "profit_share_pct": billing.profit_share_pct,
                "high_water_mark": billing.high_water_mark,
                "fees_settled": billing.fees_settled,
                "renews_on": billing.renews_on,
                "history": billing.history,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@dataclass(frozen=True, slots=True)
class FeeCalculation:
    cumulative_realized: float
    high_water_mark: float
    new_profit: float
    fee_rate_pct: float
    fee_owed: float
    fees_settled: float
    below_hwm_by: float


def calculate_fee(cumulative_realized: float, billing: Billing) -> FeeCalculation:
    """Performance fee on realized gains above the high-water mark.

    The high-water mark is what makes this honest. Without it, a client who gains
    1,000, loses 800 and gains 800 back would be charged on 1,800 of "gains" while
    actually being up 1,000 — billing them twice for recovering the same ground.
    """
    new_profit = cumulative_realized - billing.high_water_mark
    fee_owed = max(0.0, new_profit) * (billing.profit_share_pct / 100.0)
    return FeeCalculation(
        cumulative_realized=cumulative_realized,
        high_water_mark=billing.high_water_mark,
        new_profit=new_profit,
        fee_rate_pct=billing.profit_share_pct,
        fee_owed=round(fee_owed, 2),
        fees_settled=billing.fees_settled,
        below_hwm_by=abs(min(0.0, new_profit)),
    )


def settle_fee(tenant_dir: Path, billing: Billing, calculation: FeeCalculation) -> Billing:
    """Record a collected fee and raise the high-water mark to the new peak."""
    updated = Billing(
        model=billing.model,
        subscription_active=billing.subscription_active,
        monthly_price=billing.monthly_price,
        currency=billing.currency,
        profit_share_pct=billing.profit_share_pct,
        high_water_mark=calculation.cumulative_realized,
        fees_settled=round(billing.fees_settled + calculation.fee_owed, 2),
        renews_on=billing.renews_on,
        history=[
            *billing.history,
            {
                "settled_at": datetime.now(timezone.utc).isoformat(),
                "amount": calculation.fee_owed,
                "on_profit": round(calculation.new_profit, 2),
                "new_hwm": round(calculation.cumulative_realized, 2),
            },
        ],
    )
    write_billing(tenant_dir, updated)
    return updated


# --------------------------------------------------------------- הרשמה עצמית

DEMO_CLIENT_ID = "demo_showcase"


@dataclass(frozen=True, slots=True)
class Registration:
    client_id: str
    company: str
    contact: str
    email: str
    phone: str = ""
    status: str = "pending"          # pending | approved | rejected
    submitted_at: str = ""
    decided_at: str = ""
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "company": self.company,
            "contact": self.contact,
            "email": self.email,
            "phone": self.phone,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "decided_at": self.decided_at,
            "note": self.note,
        }


class RegistrationStore:
    """Inbound B2B signups awaiting operator approval.

    Self-registration deliberately does NOT create a tradeable account. It records
    an inbound lead and an inactive login. A prospect cannot provision themselves a
    trading profile: an operator has to approve them, which is when a ClientProfile
    is written. Anything else would let an anonymous form create an entity that can
    place orders.

    There is no email verification, rate limiting or CAPTCHA here. Treat this as
    lead capture on a trusted network, not as public internet-facing onboarding.
    """

    def __init__(self, data_root: Path) -> None:
        self._dir = Path(data_root) / PLATFORM_DIRNAME
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "registrations.json"

    def _load(self) -> dict[str, Registration]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        return {
            key: Registration(
                client_id=value["client_id"],
                company=value.get("company", ""),
                contact=value.get("contact", ""),
                email=value.get("email", ""),
                phone=value.get("phone", ""),
                status=value.get("status", "pending"),
                submitted_at=value.get("submitted_at", ""),
                decided_at=value.get("decided_at", ""),
                note=value.get("note", ""),
            )
            for key, value in raw.items()
        }

    def _save(self, registrations: dict[str, Registration]) -> None:
        payload = {k: v.to_json() for k, v in registrations.items()}
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(self._path)
        _restrict(self._path)

    def list_all(self) -> list[Registration]:
        return sorted(self._load().values(), key=lambda r: r.submitted_at, reverse=True)

    def pending(self) -> list[Registration]:
        return [r for r in self.list_all() if r.status == "pending"]

    def get(self, client_id: str) -> Registration | None:
        return self._load().get(client_id)

    def submit(
        self,
        *,
        client_id: str,
        company: str,
        contact: str,
        email: str,
        phone: str = "",
    ) -> Registration:
        validate_client_id(client_id)
        if client_id == DEMO_CLIENT_ID:
            raise ValueError("this identifier is reserved")
        registrations = self._load()
        if client_id in registrations:
            raise ValueError("a registration with this identifier already exists")
        if "@" not in email or "." not in email.split("@")[-1]:
            raise ValueError("invalid email address")

        registration = Registration(
            client_id=client_id,
            company=company.strip(),
            contact=contact.strip(),
            email=email.strip(),
            phone=phone.strip(),
            status="pending",
            submitted_at=datetime.now(timezone.utc).isoformat(),
        )
        registrations[client_id] = registration
        self._save(registrations)
        return registration

    def decide(self, client_id: str, status: str, note: str = "") -> Registration | None:
        registrations = self._load()
        current = registrations.get(client_id)
        if current is None:
            return None
        updated = Registration(
            client_id=current.client_id,
            company=current.company,
            contact=current.contact,
            email=current.email,
            phone=current.phone,
            status=status,
            submitted_at=current.submitted_at,
            decided_at=datetime.now(timezone.utc).isoformat(),
            note=note,
        )
        registrations[client_id] = updated
        self._save(registrations)
        return updated


PROFILE_TEMPLATE = """# נוצר אוטומטית באישור הרשמה. ערוך לפי הצורך.
client_id: {client_id}
display_name: {display_name}
timeframe: 1Day
lookback_bars: 180
max_candidates_per_cycle: 3
memory_top_k: 5
monitor_interval_seconds: 60
max_holding_days: 10
tags: [self_registered]

broker:
  name: paper
  paper: true
  credential_ref: TRADESYS__{upper_id}__ALPACA

risk:
  max_drawdown_pct: 2.0
  max_daily_loss_pct: 1.5
  risk_per_trade_pct: 0.5
  max_position_pct: 10.0
  max_open_positions: 4
  max_trades_per_day: 3
  min_reward_risk: 2.0
  allow_short: false
  allow_leverage: false
  min_price: 1.0
  max_position_pct_of_adv: 0.02
  forbidden_setups: []

watchlist:
  - symbol: AAPL
    setups: [pullback, trend_continuation]
  - symbol: MSFT
    setups: [pullback, trend_continuation]
  - symbol: NVDA
    setups: [breakout, trend_continuation]
"""


def provision_client_profile(
    clients_dir: Path, client_id: str, display_name: str
) -> Path:
    """Write a conservative starter profile for a newly approved client.

    Deliberately cautious defaults: paper broker, 2% max drawdown, no shorting, no
    leverage, three large-cap symbols. An approved client starts in the safest
    configuration the risk engine supports; widening it is an explicit later act.
    """
    validate_client_id(client_id)
    path = Path(clients_dir) / f"{client_id}.yaml"
    if path.exists():
        raise ValueError(f"a profile already exists at {path.name}")
    path.write_text(
        PROFILE_TEMPLATE.format(
            client_id=client_id,
            display_name=display_name or client_id,
            upper_id=client_id.upper(),
        ),
        encoding="utf-8",
    )
    return path
