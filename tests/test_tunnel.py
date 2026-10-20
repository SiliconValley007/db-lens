from __future__ import annotations

import shutil
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import make_url

from db_lens import store, tunnel


@pytest.mark.parametrize(
    "text,host,port",
    [
        ("bastion", "bastion", None),
        ("me@bastion", "me@bastion", None),
        ("me@bastion:2222", "me@bastion", 2222),
        ("10.0.0.5:22", "10.0.0.5", 22),
        ("me@[::1]:2200", "me@[::1]", 2200),
        ("::1", "::1", None),
        ("a.b-c_d@h.example.com", "a.b-c_d@h.example.com", None),
    ],
)
def test_parse_target(text: Any, host: Any, port: Any) -> None:
    spec = tunnel.parse_target(text, key="k", accept_new=True)
    assert (spec.host, spec.port, spec.key, spec.accept_new) == (host, port, "k", True)


@pytest.mark.parametrize("bad", ["", "  ", "-oProxyCommand=evil", "-J host", "host name", "host;rm", "host:0", "host:70000", "$(id)", "a b@c"])
def test_parse_target_refuses_anything_ssh_could_read_as_an_option(bad: Any) -> None:
    with pytest.raises(ValueError, match="--ssh"):
        tunnel.parse_target(bad)


def test_command_is_a_list_with_the_host_last_and_batch_mode() -> None:
    spec = tunnel.SshSpec("me@bastion", 2222, "/k/id", True)
    cmd = tunnel.build_command("ssh", spec, 15432, "db.internal", 5432)
    assert cmd[:4] == ["ssh", "-N", "-T", "-L"] and cmd[4] == "127.0.0.1:15432:db.internal:5432" and cmd[-1] == "me@bastion"
    assert "BatchMode=yes" in cmd and "ExitOnForwardFailure=yes" in cmd and "StrictHostKeyChecking=accept-new" in cmd
    assert cmd[cmd.index("-i") + 1] == "/k/id" and cmd[cmd.index("-p") + 1] == "2222" and "IdentitiesOnly=yes" in cmd
    plain = tunnel.build_command("ssh", tunnel.SshSpec("bastion"), 1, "h", 2)
    assert "StrictHostKeyChecking=accept-new" not in plain and "-i" not in plain and "-p" not in plain


def test_record_roundtrip() -> None:
    spec = tunnel.SshSpec("me@h", 22, None, False)
    assert spec.record() == {"host": "me@h", "port": 22} and tunnel.SshSpec.from_record(spec.record()) == spec
    assert tunnel.SshSpec.from_record({"host": "h", "accept_new": True}).accept_new and tunnel.SshSpec("h").label() == "h"


class FakeProc:
    """Stands in for `ssh -N -L`: listens on the forwarded port like a healthy tunnel, or dies with ssh's error text."""

    failure: str | None = None
    started: list[FakeProc] = []

    def __init__(self, cmd: Any, stderr: Any = None, **kw: Any) -> None:
        self.cmd, self.kw = cmd, kw
        self.returncode: int | None = None
        self.stopped = False
        self.sock: socket.socket | None = None
        FakeProc.started.append(self)
        if FakeProc.failure:
            stderr.write(FakeProc.failure.encode())
            self.returncode = 255
            return
        port = int(cmd[cmd.index("-L") + 1].split(":")[1])
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(5)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> Any:
        while self.sock:
            try:
                c, _ = self.sock.accept()
                c.close()
            except OSError:
                return

    def poll(self) -> Any:
        return self.returncode

    def terminate(self) -> Any:
        self.stopped, self.returncode = True, -15
        if self.sock:
            self.sock.close()
            self.sock = None

    kill = terminate

    def wait(self, timeout: Any = None) -> Any:
        return self.returncode


@pytest.fixture
def fake_ssh(monkeypatch: pytest.MonkeyPatch) -> Any:
    FakeProc.failure, FakeProc.started = None, []
    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ssh")
    return FakeProc


def test_postgres_keeps_its_host_name_and_redirects_only_the_address(fake_ssh: Any) -> None:
    url = make_url("postgresql+psycopg2://u:p@db.internal/shop?sslmode=verify-full")
    with tunnel.forward(url, tunnel.SshSpec("me@bastion")) as routed:
        assert routed.host == "db.internal" and routed.port != 5432 and routed.query["hostaddr"] == "127.0.0.1"
        assert routed.query["sslmode"] == "verify-full"  # certificate checks keep the real name
        cmd = fake_ssh.started[0].cmd
        assert f"127.0.0.1:{routed.port}:db.internal:5432" in cmd
    assert fake_ssh.started[0].stopped  # tunnel closed when the block ends


@pytest.mark.parametrize(
    "uri,port",
    [
        ("mysql+pymysql://u@db.internal/d", 3306),
        ("mariadb+pymysql://u@db.internal:3307/d", 3307),
        ("oracle+oracledb://u@ora.internal/?service_name=X", 1521),
    ],
)
def test_other_engines_are_pointed_at_the_local_end(fake_ssh: Any, uri: Any, port: Any) -> None:
    with tunnel.forward(make_url(uri), tunnel.SshSpec("bastion")) as routed:
        assert routed.host == "127.0.0.1" and routed.port != port and "hostaddr" not in routed.query
        assert f":{url_host(uri)}:{port}" in fake_ssh.started[0].cmd[4]


def url_host(uri: Any) -> Any:
    return make_url(uri).host


def test_tunnel_stops_even_when_the_body_raises(fake_ssh: Any) -> None:
    with pytest.raises(RuntimeError, match="boom"), tunnel.forward(make_url("mysql+pymysql://u@h/d"), tunnel.SshSpec("b")):
        raise RuntimeError("boom")
    assert fake_ssh.started[0].stopped


@pytest.mark.parametrize(
    "stderr,hint",
    [
        ("Host key verification failed.", "--ssh-accept-new"),
        ("me@h: Permission denied (publickey).", "rejected the key"),
        ("ssh: Could not resolve hostname nope: Name or service not known", "does not resolve"),
        ("ssh: connect to host h port 22: Connection refused", "Nothing is listening"),
        ("channel 0: open failed: administratively prohibited", "forbids port forwarding"),
        ("weird", None),
    ],
)
def test_ssh_failures_become_clear_errors_with_hints(fake_ssh: Any, stderr: Any, hint: Any) -> None:
    fake_ssh.failure = stderr
    with pytest.raises(tunnel.TunnelError) as e, tunnel.forward(make_url("mysql+pymysql://u@h/d"), tunnel.SshSpec("b")):
        pytest.fail("no connection must be attempted")
    assert "ssh exited with code 255" in str(e.value) and (hint is None or hint in str(e.value))


def test_secrets_in_ssh_output_are_redacted(fake_ssh: Any) -> None:
    store._SECRETS.clear()
    store.remember("Sup3rSecretPw")
    fake_ssh.failure = "debug: user bob password Sup3rSecretPw rejected"
    with pytest.raises(tunnel.TunnelError) as e, tunnel.forward(make_url("mysql+pymysql://u@h/d"), tunnel.SshSpec("b")):
        pass
    assert "Sup3rSecretPw" not in str(e.value) and "***" in str(e.value)


def test_preconditions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SQLite"), tunnel.forward(make_url("sqlite:///x.db"), tunnel.SshSpec("b")):
        pass
    with pytest.raises(ValueError, match="needs a database port"), tunnel.forward(make_url("mssql://u@h/d"), tunnel.SshSpec("b")):
        pass
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(tunnel.TunnelError, match="OpenSSH client"), tunnel.forward(make_url("mysql+pymysql://u@h/d"), tunnel.SshSpec("b")):
        pass
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ssh")
    with (
        pytest.raises(tunnel.TunnelError, match="key file not found"),
        tunnel.forward(make_url("mysql+pymysql://u@h/d"), tunnel.SshSpec("b", key=str(tmp_path / "nokey"))),
    ):
        pass


def test_tunnel_that_never_comes_up_times_out(monkeypatch: pytest.MonkeyPatch, fake_ssh: Any) -> None:
    class Silent(FakeProc):
        def __init__(self, cmd: Any, stderr: Any = None, **kw: Any) -> None:
            self.cmd, self.returncode, self.stopped, self.sock = cmd, None, False, None  # noqa: E501
            FakeProc.started.append(self)

    monkeypatch.setattr(subprocess, "Popen", Silent)
    with (
        pytest.raises(tunnel.TunnelError, match="did not come up within"),
        tunnel.forward(make_url("mysql+pymysql://u@h/d"), tunnel.SshSpec("b"), wait=0.4),
    ):
        pass
    assert fake_ssh.started[0].stopped


# ---- OS-specific branches: each helper is tested with the Windows switch AND the POSIX switch, whatever the host is ------------------------------
def test_ssh_child_gets_no_console_window_on_windows(as_windows: pytest.MonkeyPatch) -> None:
    as_windows.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)  # exists on Windows only; mocked so Linux runs this branch
    assert tunnel._is_windows() is True and tunnel._creation_flags() == 0x08000000


def test_creation_flags_default_to_zero_on_windows_when_the_constant_is_missing(as_windows: pytest.MonkeyPatch) -> None:
    as_windows.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    assert tunnel._creation_flags() == 0


def test_ssh_child_has_no_creation_flags_on_posix_even_if_the_constant_exists(as_posix: pytest.MonkeyPatch) -> None:
    as_posix.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)  # a Windows-only constant must not leak into POSIX Popen
    assert tunnel._is_windows() is False and tunnel._creation_flags() == 0


def test_tunnel_is_windows_follows_sys_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the simulation fixtures must not hide the real helper (DBLENS_SIMULATE_OS)
    monkeypatch.setattr(tunnel.sys, "platform", "win32")  # type: ignore[attr-defined]
    assert tunnel._is_windows() is True
    monkeypatch.setattr(tunnel.sys, "platform", "linux")  # type: ignore[attr-defined]
    assert tunnel._is_windows() is False


def test_popen_receives_the_windows_creation_flags(as_windows: pytest.MonkeyPatch, fake_ssh: Any) -> None:
    as_windows.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    with tunnel.forward(make_url("postgresql://u@db:5432/x"), tunnel.SshSpec("bastion")):
        assert fake_ssh.started[0].kw["creationflags"] == 0x08000000


def test_popen_receives_zero_creation_flags_on_posix(as_posix: pytest.MonkeyPatch, fake_ssh: Any) -> None:
    with tunnel.forward(make_url("postgresql://u@db:5432/x"), tunnel.SshSpec("bastion")):
        assert fake_ssh.started[0].kw["creationflags"] == 0
