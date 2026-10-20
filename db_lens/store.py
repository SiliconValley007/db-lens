"""Profile storage: atomic, locked and corruption-safe (engine-independent; unit-tested).
Secrets live in the OS keyring, in ${ENV} templates, or (last resort) in the 0600 config file."""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote, quote_plus

from sqlalchemy import make_url
from sqlalchemy.engine import URL


def _is_windows() -> bool:
    """The single switch every OS-specific branch in this module reads (tests flip it to exercise both paths on any platform)."""
    return sys.platform == "win32"


def _load_fcntl() -> Any:
    return None if _is_windows() else importlib.import_module("fcntl")


fcntl: Any = _load_fcntl()  # POSIX advisory locking; None on Windows (best effort there)

CONFIG = Path(os.environ["DB_LENS_CONFIG"]) if os.environ.get("DB_LENS_CONFIG") else Path.home() / ".db-lens" / "config.json"
SERVICE = "db-lens"
ENV_RE = re.compile(r"\$\$|\$\{(\w+)\}")  # `${NAME}` substitutes; `$$` is a literal `$` (only meaningful with a ${...})
URL_PW = re.compile(r"(://[^:/@\s]+:)[^\s/]*@")  # greedy to the LAST '@' before the path: a raw '@' inside the password cannot leave a tail behind
KV_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|passphrase|secret|token|api[_-]?key)\b([\"\']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&)]+)")
_SECRETS: set[str] = set()


class ConfigError(ValueError):
    """The config file exists but cannot be trusted; it is never overwritten."""


def _tighten(path: Path) -> None:
    """Best effort: a config file that group/others can read (it may hold a plaintext password) is made private (POSIX)."""
    if _is_windows():
        return  # NTFS ACLs apply; there are no POSIX permission bits to fix
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
    except OSError:
        pass


def has_ref(pw: str) -> bool:
    return any(m[1] for m in ENV_RE.finditer(pw))


def expand(s: str) -> str:
    def sub(m: re.Match[str]) -> str:
        if m[0] == "$$":
            return "$"
        if m[1] not in os.environ:
            raise ValueError(f"Environment variable {m[1]} is not set.")
        return os.environ[m[1]]

    return ENV_RE.sub(sub, s)


def remember(secret: str | None) -> None:
    """Register a resolved secret so redact() can scrub it from any message."""
    if secret and len(secret) >= 4:
        _SECRETS.update({secret, quote(secret, safe=""), quote_plus(secret)})


def redact(msg: str) -> str:
    msg = URL_PW.sub(r"\1***@", msg)
    msg = KV_SECRET.sub(r"\1\2***", msg)
    for s in _SECRETS:
        msg = msg.replace(s, "***")
    return msg


def load() -> dict[str, Any]:
    try:
        raw = CONFIG.read_text(encoding="utf-8")
        _tighten(CONFIG)
    except FileNotFoundError:
        raw = None
    try:
        cfg = {} if raw is None else json.loads(raw)
        profiles = cfg.get("profiles", {})
        if not isinstance(profiles, dict) or not all(
            isinstance(v, str) or (isinstance(v, dict) and isinstance(v.get("uri"), str)) for v in profiles.values()
        ):
            raise TypeError
    except (ValueError, TypeError, AttributeError) as e:
        raise ConfigError(f"Config file {CONFIG} is corrupt ({type(e).__name__}); repair or delete it. Not modified.") from e
    cfg.setdefault("default", None)
    # v0.1/0.2 stored bare URI strings
    cfg["profiles"] = {k: {"uri": v} if isinstance(v, str) else v for k, v in profiles.items()}
    return cfg


def _open_lockfile() -> IO[str]:
    """The lock file is created 0600 (the mode only applies on POSIX)."""
    return os.fdopen(os.open(CONFIG.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a")


@contextmanager
def _locked() -> Iterator[None]:
    """Serialise read-modify-write cycles across processes (POSIX advisory lock; best effort elsewhere)."""
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    locker = fcntl
    if locker is None:
        yield
        return
    with _open_lockfile() as f:
        locker.flock(f, locker.LOCK_EX)
        try:
            yield
        finally:
            locker.flock(f, locker.LOCK_UN)


def _fsync_dir(directory: Path) -> None:
    """Persist a rename by syncing its directory. Best effort: Windows cannot open a directory, which raises OSError."""
    try:
        dfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))  # O_DIRECTORY (POSIX) fails fast on a non-directory
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def save(cfg: dict[str, Any]) -> None:
    """Write to a private temp file in the same directory, fsync, then atomically replace."""
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG.parent, prefix=".config-", suffix=".tmp")  # mkstemp => mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(CONFIG.parent)


def _keyring() -> Any:
    try:
        import keyring

        return keyring if getattr(keyring.get_keyring(), "priority", 1) > 0 else None  # skip fail/null backends
    except Exception:  # noqa: BLE001 - not installed or no usable backend
        return None


def _put_secret(kr: Any, name: str, password: str) -> bool:
    try:
        kr.set_password(SERVICE, name, password)
        return bool(kr.get_password(SERVICE, name) == password)  # a silently discarding backend must not lose the password
    except Exception:  # noqa: BLE001
        return False


def _forget_secret(name: str) -> bool:
    if kr := _keyring():
        try:
            kr.delete_password(SERVICE, name)
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


def add(
    name: str,
    uri: str,
    schema: str | None = None,
    plaintext: bool = False,
    writable: bool | None = None,
    risks: list[dict[str, Any]] | None = None,
    tls: str | None = None,
    ssh: dict[str, Any] | None = None,
) -> str:
    """Save a profile and make it default. Returns where the secret lives: none|env|keyring|plaintext."""
    u, where = make_url(uri), "none"
    rec: dict[str, Any] = {"uri": uri}
    if u.password and has_ref(u.password):
        where = "env"
    elif u.password:
        where = "plaintext"
        kr = None if plaintext else _keyring()
        if kr and _put_secret(kr, name, u.password):
            bare = URL.create(u.drivername, u.username, None, u.host, u.port, u.database, dict(u.query))
            rec, where = {"uri": bare.render_as_string(), "keyring": True}, "keyring"
    if schema:
        rec["schema"] = schema
    if writable is not None:
        rec["writable"] = writable  # probed at connect time; drives the query-time warning
    if risks:
        rec["risks"] = risks
    if ssh:
        rec["ssh"] = ssh  # reached through `ssh -L` on every run (see tunnel.py)
    if tls:
        rec["tls"] = tls  # every later command refuses to run unless the connection is really encrypted
    with _locked():
        cfg = load()
        if cfg["profiles"].get(name, {}).get("keyring") and where != "keyring":
            _forget_secret(name)  # don't leave an orphaned secret behind
        cfg["profiles"][name], cfg["default"] = rec, name
        save(cfg)
    return where


def secret_kind(rec: dict[str, Any]) -> str:
    u = make_url(rec["uri"])
    return "keyring" if rec.get("keyring") else "none" if not u.password else "env" if has_ref(u.password) else "plaintext"


def resolve(name: str | None) -> tuple[str, dict[str, Any], URL]:
    """Return (profile name, record, URL with the real password filled in)."""
    cfg = load()
    name = name or cfg["default"]
    if name not in cfg["profiles"]:
        if name is None and cfg["profiles"]:
            raise ValueError(f"No default profile. Pass -p NAME or run: db-lens profiles use NAME (saved: {', '.join(cfg['profiles'])})")
        raise ValueError(f"Profile {name!r} not found. Run: db-lens connect <uri>")
    rec = cfg["profiles"][name]
    u = make_url(rec["uri"])
    if rec.get("keyring"):
        kr = _keyring()
        pw = kr.get_password(SERVICE, name) if kr else None
        if not pw:
            raise ValueError(f"Password for {name!r} not found in keyring. Run connect again.")
        u = u.set(password=pw)
    elif u.password and has_ref(u.password):
        u = u.set(password=expand(u.password))
    remember(u.password)
    return name, rec, u


def _missing(name: str, cfg: dict[str, Any]) -> ValueError:
    saved = ", ".join(cfg["profiles"]) or "none"
    return ValueError(f"Profile {name!r} not found (saved: {saved}).")


def use(name: str) -> None:
    """Make an existing profile the default."""
    with _locked():
        cfg = load()
        if name not in cfg["profiles"]:
            raise _missing(name, cfg)
        cfg["default"] = name
        save(cfg)


def remove(name: str) -> tuple[bool, str | None]:
    """Delete a profile and its keyring secret. Returns (secret_deleted, new_default). If the default was removed and exactly
    one profile remains it becomes the default; otherwise the default is cleared (the user must choose with `profiles use`)."""
    with _locked():
        cfg = load()
        if name not in cfg["profiles"]:
            raise _missing(name, cfg)
        rec = cfg["profiles"].pop(name)
        deleted = _forget_secret(name) if rec.get("keyring") else False
        if cfg["default"] == name:
            cfg["default"] = next(iter(cfg["profiles"])) if len(cfg["profiles"]) == 1 else None
        save(cfg)
        return deleted, cfg["default"]


def rename(old: str, new: str) -> bool:
    """Rename a profile, moving its keyring password and keeping its place and default status. Returns True if a secret moved."""
    if not new.strip() or any(ch in new for ch in "\r\n\0"):
        raise ValueError("The new profile name must be a non-empty single line.")
    with _locked():
        cfg = load()
        if old not in cfg["profiles"]:
            raise _missing(old, cfg)
        if new in cfg["profiles"]:
            raise ValueError(f"Profile {new!r} already exists.")
        moved = False
        if cfg["profiles"][old].get("keyring"):
            kr = _keyring()
            pw = kr.get_password(SERVICE, old) if kr else None
            if not (kr and pw and _put_secret(kr, new, pw)):
                raise ValueError(f"Could not move the keyring password of {old!r}; nothing was changed.")
            moved = True
        cfg["profiles"] = {(new if k == old else k): v for k, v in cfg["profiles"].items()}
        if cfg["default"] == old:
            cfg["default"] = new
        try:
            save(cfg)
        except BaseException:
            if moved:
                _forget_secret(new)
            raise
        if moved:
            _forget_secret(old)
        return moved
