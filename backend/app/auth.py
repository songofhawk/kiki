"""Password hashing and signed HttpOnly-cookie session authentication for Kiki.

Credentials are configured statically on the target machine (e.g. in `.env`)
with salted scrypt password hashes — no plaintext passwords are stored.

Hash format (colon-separated hex, safe in `.env` without `$` interpolation):
    scrypt:<n>:<r>:<p>:<salt_hex>:<derived_key_hex>
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import logging
import secrets
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "kiki_session"

SCRYPT_PREFIX = "scrypt"
DEFAULT_SCRYPT_N = 16384
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
DEFAULT_SCRYPT_DKLEN = 32
DEFAULT_SALT_BYTES = 16

# Precomputed dummy hash used when a username is not found so login timing
# stays roughly constant regardless of whether the username exists.
_DUMMY_SALT = b"kiki-dummy-salt!"
_DUMMY_DK = hashlib.scrypt(
    b"kiki-dummy-password",
    salt=_DUMMY_SALT,
    n=DEFAULT_SCRYPT_N,
    r=DEFAULT_SCRYPT_R,
    p=DEFAULT_SCRYPT_P,
    dklen=DEFAULT_SCRYPT_DKLEN,
)


def hash_password(
    password: str,
    *,
    n: int = DEFAULT_SCRYPT_N,
    r: int = DEFAULT_SCRYPT_R,
    p: int = DEFAULT_SCRYPT_P,
    dklen: int = DEFAULT_SCRYPT_DKLEN,
) -> str:
    """Hash a plaintext password using salted scrypt."""
    if not password:
        raise ValueError("Password must not be empty")
    salt = secrets.token_bytes(DEFAULT_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=64 * 1024 * 1024,
        dklen=dklen,
    )
    return f"{SCRYPT_PREFIX}:{n}:{r}:{p}:{salt.hex()}:{derived.hex()}"


def is_valid_password_hash(stored_hash: str) -> bool:
    """Return True if `stored_hash` matches the expected scrypt hash format."""
    return _parse_scrypt_hash(stored_hash) is not None


def _parse_scrypt_hash(
    stored_hash: str,
) -> Optional[tuple[int, int, int, bytes, bytes]]:
    parts = (stored_hash or "").strip().split(":")
    if len(parts) != 6 or parts[0] != SCRYPT_PREFIX:
        return None
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return None
    # Sanity bounds to prevent DoS via malicious parameters
    if n < 1024 or n > 1048576 or (n & (n - 1)) != 0:
        return None
    if r < 1 or r > 32 or p < 1 or p > 16:
        return None
    if len(salt) < 8 or len(expected) < 16:
        return None
    return n, r, p, salt, expected


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored scrypt hash in constant time."""
    parsed = _parse_scrypt_hash(stored_hash)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=64 * 1024 * 1024,
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(derived, expected)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> Optional[bytes]:
    if not data:
        return None
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding)
    except Exception:
        return None


def parse_auth_users(
    *,
    username: str = "",
    password_hash: str = "",
    users_raw: str = "",
) -> tuple[dict[str, str], bool]:
    """Parse static user credentials from config.

    Returns `(users_map, misconfigured)`.
    `users_map` maps username -> valid scrypt password hash.
    `misconfigured` is True if auth config was partially provided or invalid.
    """
    users: dict[str, str] = {}
    misconfigured = False

    clean_user = username.strip()
    clean_hash = password_hash.strip()
    if clean_user or clean_hash:
        if clean_user and clean_hash and is_valid_password_hash(clean_hash):
            users[clean_user] = clean_hash
        else:
            misconfigured = True
            logger.warning(
                "KIKI_AUTH_USERNAME / KIKI_AUTH_PASSWORD_HASH is incomplete or invalid. "
                "Generate a valid hash with: python -m app.auth hash-password"
            )

    raw_entries = users_raw.strip()
    if raw_entries:
        for raw_entry in raw_entries.split(";"):
            entry = raw_entry.strip()
            if not entry:
                continue
            if "=" not in entry:
                misconfigured = True
                logger.warning("Invalid entry in KIKI_AUTH_USERS (missing '='): %r", entry[:32])
                continue
            u, h = entry.split("=", 1)
            u = u.strip()
            h = h.strip()
            if u and is_valid_password_hash(h):
                users[u] = h
            else:
                misconfigured = True
                logger.warning("Invalid user or password hash in KIKI_AUTH_USERS for user %r", u)

    return users, misconfigured


@dataclass
class LoginAttemptState:
    failures: deque[float]
    locked_until: float = 0.0


class AuthManager:
    """Manages static-config login, signed session cookies, and login rate-limiting."""

    def __init__(
        self,
        *,
        username: str = "",
        password_hash: str = "",
        users_raw: str = "",
        secret: str = "",
        session_ttl_seconds: int = 7 * 24 * 3600,
        cookie_secure: bool = False,
        max_failed_attempts: int = 10,
        failure_window_seconds: int = 300,
        lockout_seconds: int = 60,
    ) -> None:
        users, misconfigured = parse_auth_users(
            username=username,
            password_hash=password_hash,
            users_raw=users_raw,
        )
        self._users: dict[str, str] = users
        self._misconfigured: bool = misconfigured
        self._secret: str = secret.strip()
        self.session_ttl_seconds: int = max(300, int(session_ttl_seconds))
        self.cookie_secure: bool = bool(cookie_secure)
        self._max_failed_attempts = max(1, int(max_failed_attempts))
        self._failure_window_seconds = max(10, int(failure_window_seconds))
        self._lockout_seconds = max(5, int(lockout_seconds))
        self._attempts: dict[str, LoginAttemptState] = {}

    @property
    def enabled(self) -> bool:
        """True if any authentication credential (valid or misconfigured) was configured."""
        return bool(self._users) or self._misconfigured

    @property
    def configured_users(self) -> Mapping[str, str]:
        return self._users

    def _signing_key_for(self, username: str) -> Optional[bytes]:
        stored_hash = self._users.get(username)
        if not stored_hash:
            return None
        # Bind signing key to the user's password hash so changing the password
        # immediately invalidates all previously issued session cookies.
        base = (self._secret or "kiki-default-session-key-v1").encode("utf-8")
        return hmac.new(base, stored_hash.encode("utf-8"), hashlib.sha256).digest()

    def check_rate_limit(self, client_key: str) -> Optional[int]:
        """Return remaining lockout seconds if rate-limited, else None."""
        state = self._attempts.get(client_key)
        if not state:
            return None
        now = time.monotonic()
        if state.locked_until > now:
            return max(1, int(state.locked_until - now))
        return None

    def record_failure(self, client_key: str) -> None:
        now = time.monotonic()
        state = self._attempts.setdefault(client_key, LoginAttemptState(failures=deque()))
        cutoff = now - self._failure_window_seconds
        while state.failures and state.failures[0] < cutoff:
            state.failures.popleft()
        state.failures.append(now)
        if len(state.failures) >= self._max_failed_attempts:
            state.locked_until = now + self._lockout_seconds
            state.failures.clear()

    def clear_failures(self, client_key: str) -> None:
        self._attempts.pop(client_key, None)

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """Verify username & password. Returns canonical username on success, else None."""
        clean_user = (username or "").strip()
        if not clean_user or not password:
            return None
        stored_hash = self._users.get(clean_user)
        if stored_hash is None:
            # Perform dummy scrypt work to mitigate timing-based username enumeration
            derived = hashlib.scrypt(
                password.encode("utf-8"),
                salt=_DUMMY_SALT,
                n=DEFAULT_SCRYPT_N,
                r=DEFAULT_SCRYPT_R,
                p=DEFAULT_SCRYPT_P,
                dklen=DEFAULT_SCRYPT_DKLEN,
            )
            hmac.compare_digest(derived, _DUMMY_DK)
            return None
        if verify_password(password, stored_hash):
            return clean_user
        return None

    def create_session_token(self, username: str) -> str:
        """Create an HMAC-SHA256 signed session token for `username`."""
        key = self._signing_key_for(username)
        if key is None:
            raise ValueError(f"Unknown user: {username!r}")
        now = int(time.time())
        payload = {
            "u": username,
            "iat": now,
            "exp": now + self.session_ttl_seconds,
            "v": 1,
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_b64 = _b64url_encode(payload_bytes)
        sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
        return f"{payload_b64}.{_b64url_encode(sig)}"

    def verify_session_token(self, token: Optional[str]) -> Optional[str]:
        """Validate a signed session token and return the username, or None if invalid/expired."""
        if not token or "." not in token:
            return None
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts
        payload_bytes = _b64url_decode(payload_b64)
        sig_bytes = _b64url_decode(sig_b64)
        if payload_bytes is None or sig_bytes is None:
            return None
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        username = payload.get("u")
        exp = payload.get("exp")
        if not isinstance(username, str) or not isinstance(exp, (int, float)):
            return None
        if int(time.time()) >= int(exp):
            return None
        key = self._signing_key_for(username)
        if key is None:
            return None
        expected_sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(sig_bytes, expected_sig):
            return None
        return username


def _cli_main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "hash-password"
    if cmd in ("-h", "--help", "help"):
        print("Usage:")
        print("  python -m app.auth hash-password [--username <name>]")
        print("  python -m app.auth verify <stored_hash>")
        return 0

    if cmd == "hash-password":
        username = ""
        if len(argv) >= 3 and argv[1] in ("-u", "--username"):
            username = argv[2].strip()
        try:
            pwd = getpass.getpass("输入登录密码: ")
            if not pwd:
                print("错误：密码不能为空。", file=sys.stderr)
                return 1
            confirm = getpass.getpass("再次确认密码: ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。", file=sys.stderr)
            return 1
        if pwd != confirm:
            print("错误：两次输入的密码不一致。", file=sys.stderr)
            return 1
        hashed = hash_password(pwd)
        print("\n请将以下配置添加到目标机的 .env 文件中：\n")
        if username:
            print(f"KIKI_AUTH_USERNAME={username}")
        else:
            print("KIKI_AUTH_USERNAME=admin")
        print(f"KIKI_AUTH_PASSWORD_HASH={hashed}")
        return 0

    if cmd == "verify":
        if len(argv) < 2:
            print("Usage: python -m app.auth verify <stored_hash>", file=sys.stderr)
            return 1
        stored = argv[1].strip()
        try:
            pwd = getpass.getpass("输入待校验密码: ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。", file=sys.stderr)
            return 1
        ok = verify_password(pwd, stored)
        print("密码匹配 ✓" if ok else "密码不匹配 ✗")
        return 0 if ok else 1

    print(f"未知子命令: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli_main(sys.argv[1:]))
