"""Building connection URLs from structured options (no shell-quoting problems) and mapping TLS flags to each driver.

TLS flags are translated per driver and then *verified after connecting* (db.assert_tls): drivers differ in their defaults
(PyMySQL 1.2 negotiates TLS on its own, 1.1 does not; mysqlclient never does), so the mapping alone is not trusted."""

from __future__ import annotations

import os
import re

from sqlalchemy import make_url
from sqlalchemy.engine import URL

DEFAULT_DRIVER = {
    "postgresql": "postgresql+psycopg2",
    "mysql": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "oracle": "oracle+oracledb",
    "sqlite": "sqlite",
}
SSL_MODES = ("disable", "require", "verify-ca", "verify-full")
STRICT_TLS = frozenset({"require", "verify-ca", "verify-full"})
_MYSQLDB_MODE = {"disable": "DISABLED", "require": "REQUIRED", "verify-ca": "VERIFY_CA", "verify-full": "VERIFY_IDENTITY"}
_FROM_MYSQLDB = {"REQUIRED": "require", "VERIFY_CA": "verify-ca", "VERIFY_IDENTITY": "verify-full"}
_SECRET_OPTION = re.compile(r"pass|pwd|secret|token", re.I)
# Oracle SQL Developer / EZConnect style: user/password@host[:port]/SID   (password may contain '/' and '@': the LAST '@' splits it)
_SQLDEV = re.compile(r"^(?P<user>[^/@:\s]+)/(?P<pw>.*)@(?P<rest>[^@\s]+)$", re.S)
_SYSDBA = re.compile(r"\s+as\s+sys(dba|oper|asm)\s*$", re.I)


def looks_like_sqldeveloper(text: str) -> bool:
    """True for `user/password@host:port/SID` (no scheme): the format shown in Oracle SQL Developer's connection details."""
    t = text.strip()
    return "://" not in t and bool(_SQLDEV.match(_SYSDBA.sub("", t)))


def parse_sqldeveloper(text: str, service: bool = False) -> URL:
    """`user/password@host[:port]/NAME` -> Oracle URL. NAME is a SID unless ``service`` (or the EZConnect form `@//host:port/NAME`)."""
    t = text.strip()
    if _SYSDBA.search(t):
        raise ValueError("SYSDBA/SYSOPER logins ('... as sysdba') are not supported; use a normal account.")
    m = _SQLDEV.match(t)
    if not m:
        raise ValueError("Expected user/password@host:port/SID (for example scott/tiger@db.example.com:1521/ORCL)")
    rest = m["rest"]
    if rest.startswith("//"):
        service, rest = True, rest[2:]
    hostport, _, name = rest.partition("/")
    host, port = hostport, 1521
    if hostport.startswith("["):  # [IPv6]:port
        host, _, tail = hostport[1:].partition("]")
        port = int(tail[1:]) if tail.startswith(":") and tail[1:].isdigit() else 1521
    elif ":" in hostport:
        host, _, p = hostport.rpartition(":")
        if not p.isdigit():
            raise ValueError(f"Invalid port {p!r} in {hostport!r}")
        port = int(p)
    if not host:
        raise ValueError("The host name is missing (user/password@HOST:port/SID)")
    if not name:
        raise ValueError("The SID (or service name) is missing: user/password@host:port/SID")
    if not 1 <= port <= 65535:
        raise ValueError(f"Port out of range: {port}")
    return URL.create(
        "oracle+oracledb",
        username=m["user"],
        password=m["pw"],
        host=host,
        port=port,
        database=None if service else name,
        query={"service_name": name} if service else {},
    )


def parse_options(pairs: list[str] | None) -> dict[str, str]:
    """['key=value', ...] -> dict. Secrets are refused here: they would be stored and printed in the clear."""
    out: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--option expects KEY=VALUE, got {pair!r}")
        if _SECRET_OPTION.search(key):
            raise ValueError(f"--option {key!r} looks like a secret; use --ask-password, --password-env or --password-stdin")
        out[key.strip()] = value
    return out


def ssl_query(drivername: str, mode: str | None, ca: str | None = None, cert: str | None = None, key: str | None = None) -> dict[str, str]:
    """URL query parameters that request ``mode`` from the given SQLAlchemy driver ({} when nothing was asked for)."""
    if mode is None and not (ca or cert or key):
        return {}
    mode = mode or ("verify-full" if ca else "require")
    if mode not in SSL_MODES:
        raise ValueError(f"--ssl-mode must be one of {', '.join(SSL_MODES)}")
    backend, _, driver = drivername.partition("+")
    q: dict[str, str] = {}
    if backend == "postgresql":  # libpq understands all four modes and the three file options natively
        q["sslmode"] = mode
        q.update({k: v for k, v in (("sslrootcert", ca), ("sslcert", cert), ("sslkey", key)) if v})
        return q
    if backend not in ("mysql", "mariadb"):
        raise ValueError(f"--ssl-* flags are not supported for {backend}; pass driver options with --option (Oracle TLS: --option protocol=tcps)")
    driver = driver or "mysqldb"
    verify = mode in ("verify-ca", "verify-full")
    if driver in ("pymysql", "mysqlconnector") and verify and not ca:
        raise ValueError(f"{driver} cannot verify the server certificate without --ssl-ca")
    if driver == "pymysql":
        if mode == "disable":
            q["ssl_disabled"] = "true"
        else:
            q["ssl_check_hostname"] = "true" if mode == "verify-full" else "false"
    elif driver == "mysqldb":  # mysqlclient
        q["ssl_mode"] = _MYSQLDB_MODE[mode]
    elif driver == "mysqlconnector":
        if mode == "disable":
            q["ssl_disabled"] = "true"
        elif verify:
            q["ssl_verify_cert"] = "true"
            if mode == "verify-full":
                q["ssl_verify_identity"] = "true"
    else:
        raise ValueError(f"--ssl-* flags are not supported for driver {driver!r}; pass its own options with --option")
    if mode != "disable":
        q.update({k: v for k, v in (("ssl_ca", ca), ("ssl_cert", cert), ("ssl_key", key)) if v})
    return q


def required_tls(url: URL, mode: str | None) -> str | None:
    """The TLS level the profile demands on every connection (None = not demanded)."""
    if mode in STRICT_TLS:
        return mode
    q = dict(url.query)
    if url.get_backend_name() == "postgresql" and q.get("sslmode") in STRICT_TLS:
        return str(q["sslmode"])
    return _FROM_MYSQLDB.get(str(q.get("ssl_mode", "")))


def _absolute_sqlite(url: URL) -> URL:
    """A relative SQLite path would point somewhere else after `cd`: store the absolute path."""
    db = url.database
    if url.get_backend_name() == "sqlite" and db and db != ":memory:" and not db.startswith("file:"):
        return url.set(database=os.path.abspath(os.path.expanduser(db)))
    return url


def build_url(
    *,
    uri: str | None = None,
    oracle_service: bool = False,
    dialect: str | None = None,
    driver: str | None = None,
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    database: str | None = None,
    password: str | None = None,
    ssl_mode: str | None = None,
    ssl_ca: str | None = None,
    ssl_cert: str | None = None,
    ssl_key: str | None = None,
    options: dict[str, str] | None = None,
) -> URL:
    """Assemble the final URL from either a URI or structured parts (values are escaped by SQLAlchemy, never by the shell)."""
    structured = any(v is not None for v in (dialect, driver, host, port, user, database))
    if uri and structured:
        raise ValueError("Give a connection URI or --dialect/--host/--database/... options, not both.")
    if uri:
        url = parse_sqldeveloper(uri, oracle_service) if looks_like_sqldeveloper(uri) else make_url(uri.strip())
        if password is not None:
            url = url.set(password=password)
    else:
        if not dialect:
            raise ValueError("Give a connection URI (argument, --uri-env, --uri-file or '-' for stdin) or --dialect with --host/--database.")
        if dialect not in DEFAULT_DRIVER:
            raise ValueError(f"--dialect must be one of {', '.join(DEFAULT_DRIVER)}")
        if dialect == "sqlite":
            if not database:
                raise ValueError("--database (the file path) is required for sqlite")
            if any(v is not None for v in (host, port, user, password)):
                raise ValueError("sqlite takes only --database")
            url = URL.create("sqlite", database=database)
        else:
            if not host:
                raise ValueError("--host is required")
            name = f"{dialect}+{driver}" if driver else DEFAULT_DRIVER[dialect]
            url = URL.create(name, username=user, password=password, host=host, port=port, database=database)
    query = {**ssl_query(url.drivername, ssl_mode, ssl_ca, ssl_cert, ssl_key), **(options or {})}
    if query:
        url = url.update_query_dict(query)
    return _absolute_sqlite(url)
