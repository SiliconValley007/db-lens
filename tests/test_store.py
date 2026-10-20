import json
import os
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import make_url

from db_lens import store

from .conftest import FakeKeyring
from .fakes import assert_mode


def test_keyring_roundtrip_leaves_no_secret_on_disk(home: Path, keyring: Any) -> None:
    assert store.add("prod", "postgresql://u:s3cr%40t@h/d", schema="sales") == "keyring"
    assert "s3cr" not in store.CONFIG.read_text()
    name, rec, u = store.resolve(None)
    assert (name, rec["schema"], u.password) == ("prod", "sales", "s3cr@t")
    assert_mode(store.CONFIG, 0o600)
    assert store.secret_kind(rec) == "keyring"


def test_lossy_keyring_backend_falls_back_instead_of_losing_the_password(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "_keyring", lambda: FakeKeyring(lossy=True))
    assert store.add("a", "postgresql://u:pw123456@h/d") == "plaintext"
    assert store.resolve("a")[2].password == "pw123456"


def test_keyring_exceptions_fall_back(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(FakeKeyring):
        def set_password(self, *a: Any) -> Any:
            raise RuntimeError("no backend")

    monkeypatch.setattr(store, "_keyring", lambda: Broken())
    assert store.add("a", "postgresql://u:pw@h/d") == "plaintext"


def test_plaintext_flag_and_secret_kinds(home: Path, keyring: Any) -> None:
    assert store.add("b", "postgresql://u:pw@h/d", plaintext=True) == "plaintext"
    assert store.secret_kind(store.load()["profiles"]["b"]) == "plaintext"
    assert store.add("n", "sqlite:///x.db") == "none"
    assert store.add("e", "postgresql://u:${PW}@h/d") == "env"
    assert store.secret_kind(store.load()["profiles"]["e"]) == "env"


def test_overwriting_a_keyring_profile_removes_the_orphaned_secret(home: Path, keyring: Any) -> None:
    store.add("p", "postgresql://u:pw@h/d")
    assert keyring.d
    store.add("p", "postgresql://u:${PW}@h/d")
    assert not keyring.d


@pytest.mark.parametrize(
    "pw,env,expected",
    [
        ("${PW}", {"PW": "x@y"}, "x@y"),
        ("pre-${PW}-post", {"PW": "mid"}, "pre-mid-post"),
        ("${A}${B}", {"A": "1", "B": "2"}, "12"),
        ("cost$$${PW}", {"PW": "9"}, "cost$9"),  # $$ is an escaped literal dollar
        ("${PW}$$", {"PW": "9"}, "9$"),
        ("${PW}", {"PW": "has ${nested} and $$"}, "has ${nested} and $$"),  # values are never re-expanded
    ],
)
def test_env_templates(home: Path, monkeypatch: pytest.MonkeyPatch, pw: Any, env: Any, expected: Any) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    store.add("e", f"postgresql://u:{pw}@h/d")
    assert store.resolve("e")[2].password == expected


def test_literal_dollars_are_not_expanded_without_a_reference(home: Path) -> None:
    store.add("lit", "postgresql://u:pa%24%24word@h/d", plaintext=True)
    assert store.resolve("lit")[2].password == "pa$$word"


def test_missing_env_var_error_names_the_variable_not_the_secret(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE", raising=False)
    store.add("e", "postgresql://u:abc${NOPE}@h/d")
    with pytest.raises(ValueError, match="NOPE") as e:
        store.resolve("e")
    assert "abc" not in str(e.value)


def test_redact_scrubs_url_passwords_and_known_secrets() -> None:
    store._SECRETS.clear()
    store.remember("S3cr@t!")
    msg = "could not connect to postgresql://bob:hunter22@db/x; also S3cr@t! and S3cr%40t%21"
    out = store.redact(msg)
    assert "hunter22" not in out and "S3cr" not in out and "bob" in out
    store.remember("ab")  # too short to scrub safely (would mangle ordinary text)
    assert store.redact("ab cd") == "ab cd"


def test_resolve_registers_secret_for_redaction(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PW", "topsecret-9")
    store.add("e", "postgresql://u:${PW}@h/d")
    store.resolve("e")
    assert "topsecret-9" not in store.redact("driver said: topsecret-9")


def test_atomic_save_failure_keeps_old_config_and_leaves_no_temp_files(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.add("a", "sqlite:///a.db")
    before = store.CONFIG.read_text()
    monkeypatch.setattr("json.dump", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.add("b", "sqlite:///b.db")
    assert store.CONFIG.read_text() == before
    assert [p.name for p in store.CONFIG.parent.iterdir() if p.suffix == ".tmp"] == []


def test_replace_failure_cleans_up_and_preserves_original(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.add("a", "sqlite:///a.db")
    before = store.CONFIG.read_text()
    monkeypatch.setattr("os.replace", lambda *a: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        store.save({"default": None, "profiles": {}})
    assert store.CONFIG.read_text() == before
    assert not [p for p in store.CONFIG.parent.iterdir() if p.suffix == ".tmp"]


def test_config_and_dir_permissions(home: Path) -> None:
    store.add("a", "sqlite:///a.db")
    assert_mode(store.CONFIG, 0o600)
    assert_mode(store.CONFIG.parent, 0o700)


@pytest.mark.parametrize("content", ["{not json", "", "[]", '{"profiles": []}', '{"profiles": "x"}'])
def test_corrupt_config_is_reported_and_never_overwritten(home: Path, content: Any) -> None:
    store.CONFIG.parent.mkdir(parents=True)
    store.CONFIG.write_text(content)
    with pytest.raises(store.ConfigError, match="corrupt"):
        store.load()
    with pytest.raises(store.ConfigError):
        store.add("x", "sqlite:///x.db")
    assert store.CONFIG.read_text() == content  # user data preserved for manual repair


def test_missing_config_is_empty_and_legacy_format_migrates(home: Path) -> None:
    assert store.load() == {"default": None, "profiles": {}}
    store.CONFIG.parent.mkdir(parents=True)
    store.CONFIG.write_text(json.dumps({"default": "x", "profiles": {"x": "sqlite:///a.db"}}))
    assert store.resolve(None)[1]["uri"] == "sqlite:///a.db"


def test_resolve_errors(home: Path, keyring: Any) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.resolve("ghost")
    store.add("k", "postgresql://u:pw@h/d")
    keyring.d.clear()
    with pytest.raises(ValueError, match="keyring"):
        store.resolve("k")


def test_keyring_helper_degrades_when_package_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda n, *a, **k: (_ for _ in ()).throw(ImportError) if n == "keyring" else real(n, *a, **k))
    assert store._keyring() is None


def test_keyring_helper_skips_null_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    fake = types.SimpleNamespace(get_keyring=lambda: types.SimpleNamespace(priority=-1))
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert store._keyring() is None
    fake.get_keyring = lambda: types.SimpleNamespace(priority=5)
    assert store._keyring() is fake


def test_lock_file_serialises_without_error(home: Path) -> None:
    store.add("a", "sqlite:///a.db")
    store.add("b", "sqlite:///b.db")
    assert set(store.load()["profiles"]) == {"a", "b"}
    assert os.path.exists(store.CONFIG.with_suffix(".lock")) or store.fcntl is None


def test_url_helper_is_sqlalchemy_url() -> None:
    assert make_url("sqlite:///x.db").get_backend_name() == "sqlite"


def test_remove_deletes_profile_and_keyring_secret_and_fixes_default(home: Path, keyring: Any) -> None:
    store.add("a", "postgresql://u:pw@h/d")
    store.add("b", "sqlite:///b.db")
    store.add("c", "sqlite:///c.db")  # default = c
    assert keyring.d
    assert store.remove("a") == (True, "c") and not keyring.d and set(store.load()["profiles"]) == {"b", "c"}
    assert store.remove("c") == (False, "b")  # default removed, exactly one left: it becomes the default
    assert store.remove("b") == (False, None) and store.load() == {"default": None, "profiles": {}}


def test_remove_default_with_several_left_clears_the_default(home: Path) -> None:
    for n in ("a", "b", "c"):
        store.add(n, f"sqlite:///{n}.db")
    assert store.remove("c") == (False, None)
    with pytest.raises(ValueError, match="No default profile.*profiles use"):
        store.resolve(None)
    store.use("a")
    assert store.resolve(None)[0] == "a"


def test_remove_and_use_unknown_profile(home: Path) -> None:
    store.add("a", "sqlite:///a.db")
    for fn in (store.remove, store.use):
        with pytest.raises(ValueError, match=r"'zzz' not found \(saved: a\)"):
            fn("zzz")
    assert set(store.load()["profiles"]) == {"a"}


def test_remove_is_atomic_and_refuses_a_corrupt_config(home: Path) -> None:
    store.add("a", "sqlite:///a.db")
    store.CONFIG.write_text("{broken")
    with pytest.raises(store.ConfigError):
        store.remove("a")
    assert store.CONFIG.read_text() == "{broken"


def test_tls_requirement_is_persisted(home: Path) -> None:
    store.add("p", "postgresql://u@h/d?sslmode=require", tls="require")
    assert store.resolve("p")[1]["tls"] == "require"
    store.add("q", "sqlite:///q.db")
    assert "tls" not in store.resolve("q")[1]


def test_config_path_can_be_redirected_by_environment(tmp_path: Path) -> None:
    import subprocess

    code = "from db_lens import store; print(store.CONFIG)"
    out = subprocess.run(
        [sys.executable, "-c", code], env={**os.environ, "DB_LENS_CONFIG": str(tmp_path / "x.json")}, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == str(tmp_path / "x.json")


def test_rename_keeps_order_default_and_moves_the_keyring_secret(home: Path, keyring: Any) -> None:
    store.add("a", "sqlite:///a.db")
    store.add("b", "postgresql://u:pw@h/d")  # keyring-backed, and the default
    store.add("c", "sqlite:///c.db")
    store.use("b")
    assert store.rename("b", "prod") is True
    cfg = store.load()
    assert list(cfg["profiles"]) == ["a", "prod", "c"] and cfg["default"] == "prod"
    assert keyring.d == {("db-lens", "prod"): "pw"}  # moved, not copied: no orphan under the old name
    assert store.resolve("prod")[2].password == "pw"
    assert store.rename("a", "alpha") is False and store.load()["default"] == "prod"


def test_rename_errors_leave_everything_untouched(home: Path, keyring: Any) -> None:
    store.add("a", "postgresql://u:pw@h/d")
    store.add("b", "sqlite:///b.db")
    for old, new, msg in [("zzz", "x", "not found"), ("a", "b", "already exists"), ("a", "", "non-empty"), ("a", "x\ny", "non-empty")]:
        with pytest.raises(ValueError, match=msg):
            store.rename(old, new)
    keyring.d.clear()  # secret vanished: refuse rather than orphan the profile
    with pytest.raises(ValueError, match="Could not move the keyring password"):
        store.rename("a", "x")
    assert list(store.load()["profiles"]) == ["a", "b"]


def test_rename_cleans_up_the_new_secret_if_saving_fails(home: Path, keyring: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store.add("a", "postgresql://u:pw@h/d")
    monkeypatch.setattr(store, "save", lambda cfg: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.rename("a", "x")
    assert keyring.d == {("db-lens", "a"): "pw"} and list(store.load()["profiles"]) == ["a"]


def test_ssh_spec_is_persisted_and_resolved(home: Path) -> None:
    store.add("p", "postgresql://u@h/d", ssh={"host": "me@bastion", "port": 2222})
    assert store.resolve("p")[1]["ssh"] == {"host": "me@bastion", "port": 2222}
    store.add("q", "sqlite:///q.db")
    assert "ssh" not in store.resolve("q")[1]


# ---- hardening: permissions, atomicity, redaction, record validation ---------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("could not parse postgresql://u:p@ss@host/db", "ss@host"),  # a raw '@' inside the password leaves no tail
        ("mysql://root:hunter2@127.0.0.1:3306/x failed", "hunter2"),
        ("password=abc123 failed", "abc123"),
        ("PASSWORD: 'two words' rejected", "two words"),
        ("dsn pwd=zzz9;host=h", "zzz9"),
        ("token = tok_live_999", "tok_live_999"),
        ('{"api_key": "k-123"}', "k-123"),
    ],
)
def test_redact_scrubs_every_secret_shape(raw: str, must_not_contain: str) -> None:
    assert must_not_contain not in store.redact(raw)


def test_redact_keeps_innocent_text_and_scrubs_registered_variants(home: Path) -> None:
    assert store.redact("password authentication failed for user bob") == "password authentication failed for user bob"
    store.remember("s3cr et&x")
    for variant in ("s3cr et&x", "s3cr%20et%26x", "s3cr+et%26x"):
        assert variant not in store.redact(f"boom {variant} boom")
    store.remember("abc")  # shorter than 4 characters is never registered (it would mangle unrelated text)
    assert store.redact("abc") == "abc"


def test_config_dir_file_and_lock_are_private_and_no_temp_files_remain(home: Path) -> None:
    store.add("a", "postgresql://u:pw123456@h/d", plaintext=True)
    d, lock = store.CONFIG.parent, store.CONFIG.with_suffix(".lock")
    assert_mode(d, 0o700)
    assert_mode(store.CONFIG, 0o600)
    assert lock.exists() == (store.fcntl is not None)  # the lock file exists exactly where locking does
    if lock.exists():
        assert_mode(lock, 0o600)
    assert sorted(p.name for p in d.iterdir()) == ["config.json"] + (["config.lock"] if store.fcntl is not None else [])


def test_loading_a_config_re_tightens_it_through_tighten(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.CONFIG.parent.mkdir(parents=True)
    store.CONFIG.write_text('{"profiles": {"a": {"uri": "sqlite:///x.db"}}}', encoding="utf-8")
    seen: list[Path] = []
    monkeypatch.setattr(store, "_tighten", seen.append)
    assert "a" in store.load()["profiles"]
    assert seen == [store.CONFIG]


def test_failed_save_keeps_the_old_file_and_removes_the_temp_file(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.add("a", "sqlite:///x.db")
    before = store.CONFIG.read_bytes()

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.add("b", "sqlite:///y.db")
    assert store.CONFIG.read_bytes() == before
    assert [p.name for p in sorted(store.CONFIG.parent.iterdir()) if p.name != "config.lock"] == ["config.json"]  # the lock file is POSIX-only


@pytest.mark.parametrize(
    "raw",
    ['{"profiles": {"a": 5}}', '{"profiles": {"a": {"no_uri": 1}}}', '{"profiles": {"a": {"uri": 7}}}', '{"profiles": []}', "[1]", "{broken"],
)
def test_malformed_profile_records_are_rejected_not_crashed_on(home: Path, raw: str) -> None:
    store.CONFIG.parent.mkdir(parents=True)
    store.CONFIG.write_text(raw, encoding="utf-8")
    with pytest.raises(store.ConfigError, match="corrupt"):
        store.load()
    assert store.CONFIG.read_text(encoding="utf-8") == raw  # never overwritten


def test_config_is_utf8_regardless_of_locale(home: Path) -> None:
    store.add("café", "sqlite:///ü.db")
    assert "café" in json.loads(store.CONFIG.read_text(encoding="utf-8"))["profiles"]
    assert "café" in store.load()["profiles"]


# ---- remaining branches: best-effort paths must never raise ------------------------------------------------------------
class FakePath:
    """Just enough of pathlib.Path for _tighten: reports a mode, records chmod calls (so the POSIX branch runs on any OS)."""

    def __init__(self, mode: int, fail: bool = False) -> None:
        self.mode, self.fail = mode, fail
        self.chmods: list[int] = []

    def stat(self) -> Any:
        if self.fail:
            raise OSError("gone")
        return types.SimpleNamespace(st_mode=self.mode)

    def chmod(self, mode: int) -> None:
        self.chmods.append(mode)


# Every helper below is tested twice on purpose: with the POSIX switch (as_posix: `_is_windows() -> False`, fcntl present, POSIX syscalls mocked)
# and with the Windows switch (as_windows), so a windows-latest run executes 100 % of BOTH branches and a Linux run does as well.
@pytest.mark.parametrize(("mode", "expected"), [(0o100644, [0o600]), (0o100640, [0o600]), (0o100604, [0o600]), (0o100600, []), (0o100400, [])])
def test_tighten_chmods_only_group_or_other_accessible_files_on_posix(as_posix: pytest.MonkeyPatch, mode: int, expected: list[int]) -> None:
    assert store._is_windows() is False
    fp = FakePath(mode)
    store._tighten(cast(Path, fp))
    assert fp.chmods == expected


def test_tighten_swallows_oserror_on_posix(as_posix: pytest.MonkeyPatch) -> None:
    store._tighten(cast(Path, FakePath(0o100644, fail=True)))  # a vanished file is not an error


def test_tighten_is_a_noop_on_windows(as_windows: pytest.MonkeyPatch) -> None:
    assert store._is_windows() is True
    fp = FakePath(0o100644)
    store._tighten(cast(Path, fp))
    assert fp.chmods == []  # NTFS: nothing to fix


def test_is_windows_follows_sys_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the simulation fixtures must not hide the real helper (DBLENS_SIMULATE_OS)
    monkeypatch.setattr(store.sys, "platform", "win32")  # type: ignore[attr-defined]
    assert store._is_windows() is True
    monkeypatch.setattr(store.sys, "platform", "linux")  # type: ignore[attr-defined]
    assert store._is_windows() is False


def test_fcntl_is_loaded_on_posix(as_posix: pytest.MonkeyPatch) -> None:
    fake = types.SimpleNamespace(name="fcntl")
    as_posix.setattr("importlib.import_module", lambda name: fake)
    assert store._load_fcntl() is fake


def test_fcntl_is_absent_on_windows_and_never_imported(as_windows: pytest.MonkeyPatch) -> None:
    def forbidden(name: str) -> Any:
        raise AssertionError("fcntl must not be imported on Windows")

    as_windows.setattr("importlib.import_module", forbidden)
    assert store._load_fcntl() is None


class FakeFcntl:
    LOCK_EX, LOCK_UN = 2, 8

    def __init__(self) -> None:
        self.calls: list[int] = []

    def flock(self, f: Any, op: int) -> None:
        self.calls.append(op)


def test_open_lockfile_is_private_append_text_mode_on_posix(as_posix: pytest.MonkeyPatch, home: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_open(path: Any, flags: int, mode: int = 0o777) -> int:
        seen["open"] = (Path(path).name, flags, mode)
        return 41

    def fake_fdopen(fd: int, mode: str = "r", *a: Any, **k: Any) -> str:
        seen["fdopen"] = (fd, mode)
        return "handle"

    as_posix.setattr(os, "open", fake_open)
    as_posix.setattr(os, "fdopen", fake_fdopen)
    assert store._open_lockfile() == "handle"  # type: ignore[comparison-overlap]
    name, flags, mode = seen["open"]
    assert name == "config.lock" and mode == 0o600 and flags == os.O_WRONLY | os.O_CREAT | os.O_APPEND and seen["fdopen"] == (41, "a")


def test_posix_locking_takes_and_releases_an_exclusive_lock_on_a_private_file(as_posix: pytest.MonkeyPatch, home: Path) -> None:
    fake = FakeFcntl()
    as_posix.setattr(store, "fcntl", fake)
    opened: list[tuple[Any, int, int]] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, **k: Any) -> int:
        opened.append((Path(path).name, flags, mode))
        return real_open(path, flags, mode)

    as_posix.setattr(os, "open", spy)
    store.add("a", "sqlite:///a.db")
    assert fake.calls[:2] == [FakeFcntl.LOCK_EX, FakeFcntl.LOCK_UN]  # locked, then released, around the write
    name, flags, mode = next(o for o in opened if o[0] == "config.lock")
    assert mode == 0o600 and flags & os.O_CREAT and flags & os.O_APPEND and flags & os.O_WRONLY
    assert store.CONFIG.with_suffix(".lock").exists()


def test_lock_is_released_even_when_the_protected_block_raises(as_posix: pytest.MonkeyPatch, home: Path) -> None:
    fake = FakeFcntl()
    as_posix.setattr(store, "fcntl", fake)
    with pytest.raises(RuntimeError), store._locked():
        raise RuntimeError("inside")
    assert fake.calls == [FakeFcntl.LOCK_EX, FakeFcntl.LOCK_UN]


def test_windows_locking_is_a_noop_and_creates_no_lock_file(as_windows: pytest.MonkeyPatch, home: Path) -> None:
    store.add("a", "sqlite:///a.db")
    assert "a" in store.load()["profiles"] and not store.CONFIG.with_suffix(".lock").exists()


def test_config_directory_is_created_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "CONFIG", tmp_path / "d" / "config.json")
    modes: list[int] = []
    real_mkdir = Path.mkdir

    def spy(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        modes.append(mode)
        real_mkdir(self, mode, parents, exist_ok)

    monkeypatch.setattr(Path, "mkdir", spy)
    store.add("a", "sqlite:///a.db")
    assert modes and set(modes) == {0o700}
    assert_mode(store.CONFIG.parent, 0o700)


def _record_dir_syscalls(mp: pytest.MonkeyPatch, calls: list[tuple[str, Any]]) -> None:
    def fake_open(path: Any, flags: int, *a: Any, **k: Any) -> int:
        calls.append(("open", flags))
        return 77

    def fake_fsync(fd: int) -> None:
        calls.append(("fsync", fd))

    def fake_close(fd: int) -> None:
        calls.append(("close", fd))

    mp.setattr(os, "open", fake_open)
    mp.setattr(os, "fsync", fake_fsync)
    mp.setattr(os, "close", fake_close)


def test_directory_fsync_opens_with_o_directory_syncs_and_closes_on_posix(as_posix: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, Any]] = []
    as_posix.setattr(os, "O_DIRECTORY", 0x10000, raising=False)  # exists on POSIX only; mocked so Windows runs this branch too
    _record_dir_syscalls(as_posix, calls)
    store._fsync_dir(tmp_path)
    assert calls == [("open", os.O_RDONLY | 0x10000), ("fsync", 77), ("close", 77)]


def test_directory_fsync_without_o_directory_still_syncs(as_posix: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, Any]] = []
    as_posix.delattr(os, "O_DIRECTORY", raising=False)
    _record_dir_syscalls(as_posix, calls)
    store._fsync_dir(tmp_path)
    assert calls == [("open", os.O_RDONLY), ("fsync", 77), ("close", 77)]


@pytest.mark.parametrize("where", ["open", "fsync"])
def test_directory_fsync_is_best_effort_and_never_leaks_the_descriptor_on_posix(as_posix: pytest.MonkeyPatch, tmp_path: Path, where: str) -> None:
    closed: list[int] = []

    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("not supported here")

    def fake_close(fd: int) -> None:
        closed.append(fd)

    as_posix.setattr(os, "open", boom if where == "open" else lambda *a, **k: 5)
    as_posix.setattr(os, "fsync", boom)
    as_posix.setattr(os, "close", fake_close)
    store._fsync_dir(tmp_path)  # must not raise
    assert closed == ([] if where == "open" else [5])


def test_directory_fsync_on_windows_is_a_silent_noop_when_directories_cannot_be_opened(as_windows: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise PermissionError("Windows cannot open a directory")

    as_windows.delattr(os, "O_DIRECTORY", raising=False)  # it does not exist there
    as_windows.setattr(os, "open", boom)
    store._fsync_dir(tmp_path)  # must not raise


def test_forgetting_a_secret_survives_every_keyring_failure(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert store._forget_secret("a") is False  # no usable keyring

    class Broken(FakeKeyring):
        def delete_password(self, s: str, n: str) -> None:
            raise RuntimeError("locked")

    monkeypatch.setattr(store, "_keyring", lambda: Broken())
    assert store._forget_secret("a") is False


def test_ssh_record_is_stored_with_the_profile(home: Path) -> None:
    store.add("a", "postgresql://u@h/d", ssh={"host": "bastion"})
    assert store.load()["profiles"]["a"]["ssh"] == {"host": "bastion"}


def test_expand_and_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBL_X", "v")
    assert store.expand("a${DBL_X}$$") == "av$"
    monkeypatch.delenv("DBL_X")
    with pytest.raises(ValueError, match="DBL_X is not set"):
        store.expand("${DBL_X}")


def test_risks_are_stored_with_the_profile(home: Path) -> None:
    store.add("a", "postgresql://u@h/d", risks=[{"kind": "custom-routine", "message": "m"}])
    assert store.load()["profiles"]["a"]["risks"][0]["kind"] == "custom-routine"
