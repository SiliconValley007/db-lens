from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import make_url

from db_lens import conn


@pytest.mark.parametrize(
    "name,mode,ca,expected",
    [
        ("postgresql+psycopg2", "require", None, {"sslmode": "require"}),
        ("postgresql+psycopg2", "verify-full", "ca.pem", {"sslmode": "verify-full", "sslrootcert": "ca.pem"}),
        ("postgresql", "disable", None, {"sslmode": "disable"}),
        ("mysql+pymysql", "require", None, {"ssl_check_hostname": "false"}),
        ("mysql+pymysql", "verify-ca", "ca.pem", {"ssl_check_hostname": "false", "ssl_ca": "ca.pem"}),
        ("mariadb+pymysql", "verify-full", "ca.pem", {"ssl_check_hostname": "true", "ssl_ca": "ca.pem"}),
        ("mysql+pymysql", "disable", None, {"ssl_disabled": "true"}),
        ("mysql+mysqldb", "require", None, {"ssl_mode": "REQUIRED"}),
        ("mysql", "verify-full", "ca.pem", {"ssl_mode": "VERIFY_IDENTITY", "ssl_ca": "ca.pem"}),  # no driver = mysqlclient
        ("mysql+mysqldb", "disable", None, {"ssl_mode": "DISABLED"}),
        ("mysql+mysqlconnector", "require", None, {}),
        ("mysql+mysqlconnector", "verify-ca", "ca.pem", {"ssl_verify_cert": "true", "ssl_ca": "ca.pem"}),
        ("mysql+mysqlconnector", "verify-full", "ca.pem", {"ssl_verify_cert": "true", "ssl_verify_identity": "true", "ssl_ca": "ca.pem"}),
        ("mysql+mysqlconnector", "disable", None, {"ssl_disabled": "true"}),
    ],
)
def test_ssl_query_per_driver(name: Any, mode: Any, ca: Any, expected: Any) -> None:
    assert conn.ssl_query(name, mode, ca) == expected


def test_ssl_query_client_certificates_and_implied_mode() -> None:
    assert conn.ssl_query("postgresql", None, "ca", "c.pem", "k.pem") == {
        "sslmode": "verify-full",
        "sslrootcert": "ca",
        "sslcert": "c.pem",
        "sslkey": "k.pem",
    }
    assert conn.ssl_query("mysql+pymysql", None, None, "c.pem", "k.pem") == {"ssl_check_hostname": "false", "ssl_cert": "c.pem", "ssl_key": "k.pem"}
    assert conn.ssl_query("mysql+pymysql", "disable", None, "c.pem", "k.pem") == {"ssl_disabled": "true"}  # no certs when disabled
    assert conn.ssl_query("postgresql", None) == {}


@pytest.mark.parametrize(
    "name,msg",
    [
        ("mysql+pymysql", "without --ssl-ca"),
        ("mysql+mysqlconnector", "without --ssl-ca"),
        ("sqlite", "not supported for sqlite"),
        ("oracle+oracledb", "protocol=tcps"),
        ("mysql+asyncmy", "not supported for driver"),
    ],
)
def test_ssl_query_refuses_what_it_cannot_guarantee(name: Any, msg: Any) -> None:
    with pytest.raises(ValueError, match=msg):
        conn.ssl_query(name, "verify-full" if "mysql" in name else "require")


def test_ssl_mode_must_be_known() -> None:
    with pytest.raises(ValueError, match="--ssl-mode"):
        conn.ssl_query("postgresql", "sometimes")


def test_parse_options() -> None:
    assert conn.parse_options(["service_name=XE", "a=b=c", "x="]) == {"service_name": "XE", "a": "b=c", "x": ""}
    assert conn.parse_options(None) == {}
    for bad in ("novalue", "=v"):
        with pytest.raises(ValueError, match="KEY=VALUE"):
            conn.parse_options([bad])
    for secret in ("password=x", "DB_PASSWD=x", "api_token=1", "client_secret=2", "pwd=3"):
        with pytest.raises(ValueError, match="secret"):
            conn.parse_options([secret])


def test_build_url_from_parts_escapes_instead_of_asking_the_shell() -> None:
    u = conn.build_url(
        dialect="postgresql",
        host="db.example.com",
        port=5433,
        user="me@corp",
        database="shop",
        password="p@ss:w/ord$&;!%",
        ssl_mode="verify-full",
        ssl_ca="C:\\certs\\ca.pem",
    )
    assert u.drivername == "postgresql+psycopg2" and u.password == "p@ss:w/ord$&;!%" and u.username == "me@corp"
    again = make_url(u.render_as_string(hide_password=False))  # survives a round trip through the stored string
    assert again.password == "p@ss:w/ord$&;!%" and again.query["sslrootcert"] == "C:\\certs\\ca.pem" and again.port == 5433


@pytest.mark.parametrize(
    "dialect,driver,expected",
    [
        ("mysql", None, "mysql+pymysql"),
        ("mariadb", None, "mariadb+pymysql"),
        ("mysql", "mysqldb", "mysql+mysqldb"),
        ("oracle", None, "oracle+oracledb"),
        ("postgresql", "psycopg", "postgresql+psycopg"),
    ],
)
def test_build_url_driver_defaults(dialect: Any, driver: Any, expected: Any) -> None:
    assert conn.build_url(dialect=dialect, driver=driver, host="h", database="d").drivername == expected


def test_build_url_options_and_uri_merging() -> None:
    u = conn.build_url(dialect="oracle", host="h", port=1521, user="u", options={"service_name": "XE", "protocol": "tcps"})
    assert dict(u.query) == {"service_name": "XE", "protocol": "tcps"}
    u = conn.build_url(uri="postgresql://u@h/d", ssl_mode="require", options={"application_name": "dbl"}, password="pw")
    assert u.password == "pw" and dict(u.query) == {"sslmode": "require", "application_name": "dbl"}


def test_build_url_validation() -> None:
    for kw, msg in [
        (dict(), "Give a connection URI"),
        (dict(uri="sqlite://", host="h"), "not both"),
        (dict(dialect="nope"), "--dialect must be"),
        (dict(dialect="sqlite"), "--database"),
        (dict(dialect="sqlite", database="x", host="h"), "only --database"),
        (dict(dialect="postgresql"), "--host"),
    ]:
        with pytest.raises(ValueError, match=msg):
            conn.build_url(**cast(dict[str, Any], kw))


def test_sqlite_paths_become_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert conn.build_url(dialect="sqlite", database="shop.db").database == str(tmp_path / "shop.db")
    assert conn.build_url(uri="sqlite:///./rel.db").database == str(tmp_path / "rel.db")
    assert conn.build_url(uri="sqlite://").database is None
    assert conn.build_url(uri="sqlite:///:memory:").database == ":memory:"
    assert conn.build_url(uri="sqlite:///file:x.db?uri=true").database == "file:x.db"


@pytest.mark.parametrize(
    "uri,mode,expected",
    [
        ("postgresql://u@h/d?sslmode=verify-ca", None, "verify-ca"),
        ("postgresql://u@h/d?sslmode=prefer", None, None),
        ("postgresql://u@h/d", "require", "require"),
        ("postgresql://u@h/d", "disable", None),
        ("mysql+mysqldb://u@h/d?ssl_mode=VERIFY_IDENTITY", None, "verify-full"),
        ("mysql+mysqldb://u@h/d?ssl_mode=DISABLED", None, None),
        ("mysql+pymysql://u@h/d", None, None),
        ("sqlite://", None, None),
    ],
)
def test_required_tls(uri: Any, mode: Any, expected: Any) -> None:
    assert conn.required_tls(make_url(uri), mode) == expected


import pytest as _pytest  # noqa: E402

from db_lens import conn as _conn  # noqa: E402


@_pytest.mark.parametrize(
    "text,port,db,query",
    [
        ("scott/tiger@db.example.com:1521/ORCL", 1521, "ORCL", {}),
        ("scott/tiger@//db.example.com:1522/SVC", 1522, None, {"service_name": "SVC"}),
        ("scott/tiger@db.example.com/ORCL", 1521, "ORCL", {}),
        ("scott/tiger@[::1]:1530/ORCL", 1530, "ORCL", {}),
    ],
)
def test_parse_sqldeveloper_ok(text: Any, port: Any, db: Any, query: Any) -> None:
    u = _conn.parse_sqldeveloper(text)
    assert (u.port, u.database, dict(u.query)) == (port, db, query)
    assert _conn.looks_like_sqldeveloper(text)


@_pytest.mark.parametrize(
    "text",
    [
        "scott/tiger@host:1521/ORCL as sysdba",
        "nonsense",
        "scott/tiger@host:abc/ORCL",
        "scott/tiger@host:99999/ORCL",
        "scott/tiger@host:1521/",
    ],
)
def test_parse_sqldeveloper_rejects(text: Any) -> None:
    with _pytest.raises(ValueError):
        _conn.parse_sqldeveloper(text)


def test_parse_sqldeveloper_service_flag() -> None:
    assert _conn.parse_sqldeveloper("u/p@h:1521/S", service=True).query["service_name"] == "S"
