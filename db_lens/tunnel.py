"""SSH tunnels through the system OpenSSH client (`ssh -N -L`): no extra Python dependency, uses your keys, ssh-agent and known_hosts.

Safety choices: BatchMode (never prompts, so a CI job cannot hang), host keys are checked by ssh (unknown hosts fail unless the profile
was created with --ssh-accept-new), a host starting with '-' is refused (it could smuggle ssh options), and the child process is always
stopped when the command ends."""

from __future__ import annotations

import logging
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import IO, Any

from sqlalchemy.engine import URL

from . import store

log = logging.getLogger("db_lens")
DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "oracle": 1521}
_HOST = re.compile(r"^[A-Za-z0-9_:\[][A-Za-z0-9._@%:\[\]-]*$")  # never starts with "-": ssh would read it as an option
_HINTS = (
    (
        "host key verification failed",
        "The server's host key is unknown or changed. Connect once with `ssh <host>` to trust it, "
        "or create the profile with --ssh-accept-new (first contact only).",
    ),
    (
        "permission denied",
        "The SSH server rejected the key. Check --ssh-key, your ssh-agent and the user name (password logins are disabled on purpose: BatchMode).",
    ),
    ("could not resolve hostname", "The SSH host name does not resolve."),
    ("connection refused", "Nothing is listening on the SSH port."),
    ("timed out", "The SSH server did not answer in time."),
    ("remote port forwarding failed", "The local port could not be forwarded; retry."),
    ("administratively prohibited", "The SSH server forbids port forwarding (AllowTcpForwarding / PermitOpen)."),
)


class TunnelError(RuntimeError):
    """The SSH tunnel could not be established; nothing was sent to the database."""


@dataclass(frozen=True)
class SshSpec:
    host: str  # [user@]host
    port: int | None = None
    key: str | None = None
    accept_new: bool = False

    def record(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, False)}

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> SshSpec:
        return cls(host=str(rec["host"]), port=rec.get("port"), key=rec.get("key"), accept_new=bool(rec.get("accept_new", False)))

    def label(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host


def parse_target(text: str, key: str | None = None, accept_new: bool = False) -> SshSpec:
    """'[user@]host[:port]' (or '[user@][v6::addr]:port') -> SshSpec, refusing anything ssh could read as an option."""
    t = text.strip()
    port: int | None = None
    if m := re.fullmatch(r"(.+?):(\d{1,5})", t):
        if not (t.count(":") > 1 and "[" not in t):  # a bare IPv6 address has several ':' and no port
            t, port = m[1], int(m[2])
    if not t or t.startswith("-") or not _HOST.match(t):
        raise ValueError(f"--ssh expects [user@]host[:port], got {text!r}")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"--ssh port out of range: {port}")
    return SshSpec(t.strip("[]") if t.startswith("[") and "@" not in t else t, port, key, accept_new)


def build_command(ssh: str, spec: SshSpec, local_port: int, remote_host: str, remote_port: int) -> list[str]:
    cmd = [
        ssh,
        "-N",
        "-T",
        "-L",
        f"127.0.0.1:{local_port}:{remote_host}:{remote_port}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=30",
    ]
    if spec.accept_new:
        cmd += ["-o", "StrictHostKeyChecking=accept-new"]
    if spec.key:
        cmd += ["-i", spec.key, "-o", "IdentitiesOnly=yes"]
    if spec.port:
        cmd += ["-p", str(spec.port)]
    return [*cmd, spec.host]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _diagnose(err: IO[bytes], code: int | None) -> str:
    err.seek(0)
    text = store.redact(err.read().decode("utf-8", "replace").strip())
    low = text.lower()
    hint = next((h for needle, h in _HINTS if needle in low), "")
    return f"ssh exited with code {code}: {text[-400:] or 'no output'}" + (f"\n→ {hint}" if hint else "")


def _is_windows() -> bool:
    return sys.platform == "win32"


def _creation_flags() -> int:
    """Windows: no console window for the ssh child; elsewhere the flag does not exist and 0 is the neutral value."""
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if _is_windows() else 0


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


@contextmanager
def forward(url: URL, spec: SshSpec, wait: float = 20.0) -> Iterator[URL]:
    """Open a tunnel to the database host/port in ``url`` and yield the URL that reaches it through 127.0.0.1."""
    backend = url.get_backend_name()
    if backend == "sqlite":
        raise ValueError("--ssh does not apply to SQLite (it is a local file).")
    remote_port = url.port or DEFAULT_PORTS.get(backend)
    if not remote_port:
        raise ValueError(f"--ssh needs a database port for {backend}; add --port.")
    exe = shutil.which("ssh")
    if exe is None:
        raise TunnelError("The OpenSSH client `ssh` was not found on PATH (Windows 10+: Settings → Optional features → OpenSSH Client).")
    if spec.key and not _readable(spec.key):
        raise TunnelError(f"--ssh-key file not found or unreadable: {spec.key}")
    port = _free_port()
    cmd = build_command(exe, spec, port, url.host or "localhost", remote_port)
    log.debug("ssh tunnel: 127.0.0.1:%d -> %s:%d via %s", port, url.host or "localhost", remote_port, spec.label())
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, creationflags=_creation_flags())
    try:
        deadline = time.monotonic() + wait
        while True:
            if proc.poll() is not None:
                raise TunnelError(_diagnose(err, proc.returncode))
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TunnelError(f"The SSH tunnel did not come up within {wait:.0f}s.\n→ {_diagnose(err, None)}") from None
                time.sleep(0.1)
        # PostgreSQL keeps the real host name (certificate checks) and only the *address* is redirected (libpq `hostaddr`).
        routed = url.set(port=port).update_query_dict({"hostaddr": "127.0.0.1"}) if backend == "postgresql" else url.set(host="127.0.0.1", port=port)
        yield routed
    finally:
        _stop(proc)
        err.close()
        log.debug("ssh tunnel closed")


def _readable(path: str) -> bool:
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False
