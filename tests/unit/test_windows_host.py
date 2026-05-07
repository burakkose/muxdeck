"""Unit tests for Windows host detection in WSL."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from muxdeck.adapters import windows_host as mod
from muxdeck.adapters.windows_host import (
    CommandOutcome,
    detect_windows_host,
)


def _ok(stdout: str) -> CommandOutcome:
    return CommandOutcome(returncode=0, stdout=stdout)


def _fail() -> CommandOutcome:
    return CommandOutcome(returncode=127, stdout="", stderr="not found")


def test_not_wsl_returns_disabled() -> None:
    info = detect_windows_host(
        env={},
        runner=lambda _argv: _fail(),
        osrelease_reader=lambda: "5.15.0-generic",
    )
    assert info.is_wsl is False
    assert info.resolver == "none"
    assert info.session_state_dir is None


def test_detects_wsl_via_env(tmp_path: Path) -> None:
    # Arrange a fake Windows home under tmp_path/mnt/c/Users/alice
    user_home = tmp_path / "mnt" / "c" / "Users" / "alice"
    (user_home / ".copilot" / "session-state").mkdir(parents=True)

    def runner(argv: list[str]) -> CommandOutcome:
        if argv[0] == "wslvar":
            return _ok("C:\\Users\\alice\n")
        return _fail()

    # Override _winpath_to_wsl via env.USERPROFILE shortcut wouldn't help;
    # instead we test the wslvar path with a manual tmp mapping by
    # monkeypatching _winpath_to_wsl. Easier: use the env_userprofile
    # branch (USERPROFILE already set). But that still uses the fixed
    # /mnt/c translation. Test the mnt_scan resolver which takes an
    # injectable mnt_root instead.
    info = detect_windows_host(
        env={"WSL_DISTRO_NAME": "Ubuntu"},
        runner=runner,  # ignored because mnt_scan runs last after wslvar
        osrelease_reader=lambda: "",
        mnt_root=tmp_path / "mnt" / "c",
    )
    # wslvar returns C:\Users\alice, but _winpath_to_wsl always maps to
    # the real /mnt/c/... — which won't exist in tests. So detection
    # returns an error state, and only mnt_scan (with our injected root)
    # would succeed. Confirm the fallthrough lands on mnt_scan.
    assert info.is_wsl is True
    assert info.resolver == "mnt_scan"
    assert info.session_state_dir == user_home / ".copilot" / "session-state"
    assert info.is_available is True


def test_detects_wsl_via_osrelease_with_env_userprofile(tmp_path: Path) -> None:
    # env.USERPROFILE sets a path that _winpath_to_wsl can translate, but
    # the translated path needs to exist for the resolver to succeed.
    # Use a mnt_root that does not exist so every resolver fails and we
    # surface a "missing directory" error state tagged env_userprofile.
    empty_mnt = tmp_path / "nowhere"
    info = detect_windows_host(
        env={"USERPROFILE": "C:\\Users\\bob"},
        runner=lambda _argv: _fail(),
        osrelease_reader=lambda: "5.15.0-microsoft-standard-WSL2",
        mnt_root=empty_mnt,
    )
    assert info.is_wsl is True
    assert info.windows_userprofile == "C:\\Users\\bob"
    assert info.resolver == "env_userprofile"
    assert info.error is not None
    assert info.is_available is False


def test_mnt_scan_refuses_to_guess_with_multiple_candidates(tmp_path: Path) -> None:
    for name in ("alice", "bob"):
        (tmp_path / "Users" / name / ".copilot" / "session-state").mkdir(parents=True)

    info = detect_windows_host(
        env={"WSL_DISTRO_NAME": "Ubuntu"},
        runner=lambda _argv: _fail(),
        osrelease_reader=lambda: "",
        mnt_root=tmp_path,
    )
    assert info.is_wsl is True
    assert info.resolver == "none"
    assert info.error is not None


def test_cmd_exe_fallback_reports_when_wslvar_missing(tmp_path: Path) -> None:
    user_home = tmp_path / "Users" / "carol"
    (user_home / ".copilot" / "session-state").mkdir(parents=True)

    def runner(argv: list[str]) -> CommandOutcome:
        if argv[0] == "wslvar":
            return _fail()
        if argv[0] == "cmd.exe":
            # cmd.exe /c echo %USERPROFILE% — emits CRLF, so include \r.
            return _ok("C:\\Users\\carol\r\n")
        return _fail()

    info = detect_windows_host(
        env={"WSL_DISTRO_NAME": "Ubuntu"},
        runner=runner,
        osrelease_reader=lambda: "",
        mnt_root=tmp_path,
    )
    # cmd_exe maps C:\Users\carol to /mnt/c/Users/carol which won't
    # exist in tests, so detection falls through to mnt_scan, which DOES
    # find the tmp_path/Users/carol/.copilot/session-state we created.
    assert info.is_wsl is True
    assert info.resolver == "mnt_scan"
    assert info.session_state_dir == user_home / ".copilot" / "session-state"


def test_resolver_returns_error_when_cmd_unexpanded(tmp_path: Path) -> None:
    def runner(argv: list[str]) -> CommandOutcome:
        if argv[0] == "cmd.exe":
            # Non-WSL cmd may literally echo the unexpanded variable
            return _ok("%USERPROFILE%\r\n")
        return _fail()

    info = detect_windows_host(
        env={"WSL_DISTRO_NAME": "Ubuntu"},
        runner=runner,
        osrelease_reader=lambda: "",
        mnt_root=tmp_path,
    )
    assert info.is_wsl is True
    assert info.resolver == "none"
    assert info.error is not None


@pytest.mark.parametrize(
    ("winpath", "expected_prefix"),
    [
        ("C:\\Users\\foo", "/mnt/c/Users/foo"),
        ("D:/Projects/bar", "/mnt/d/Projects/bar"),
    ],
)
def test_winpath_translation_is_deterministic(winpath: str, expected_prefix: str) -> None:
    from muxdeck.adapters.windows_host import _winpath_to_wsl

    result = _winpath_to_wsl(winpath)
    assert result is not None
    assert str(result) == expected_prefix


# ── _winpath_to_wsl edge cases ───────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["", "   ", "''", '""', "X", "/usr/local"],
)
def test_winpath_returns_none_for_unsupported_inputs(value: str) -> None:
    from muxdeck.adapters.windows_host import _winpath_to_wsl

    assert _winpath_to_wsl(value) is None


def test_winpath_returns_drive_root_for_just_drive() -> None:
    from muxdeck.adapters.windows_host import _winpath_to_wsl

    result = _winpath_to_wsl("C:\\")
    assert result is not None
    assert str(result) == "/mnt/c"


# ── _is_wsl edge cases ───────────────────────────────────────────────


def test_is_wsl_swallows_osrelease_oserror() -> None:
    from muxdeck.adapters.windows_host import _is_wsl

    def boom() -> str:
        raise OSError("permission denied")

    assert _is_wsl({}, boom) is False


def test_is_wsl_detects_wsl_in_osrelease() -> None:
    from muxdeck.adapters.windows_host import _is_wsl

    assert _is_wsl({}, lambda: "5.15-WSL2-microsoft") is True
    assert _is_wsl({}, lambda: "stock") is False


# ── _default_runner exception swallowing ─────────────────────────────


def test_default_runner_returns_127_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    from muxdeck.adapters import windows_host as mod

    def boom(argv: object, /, **kwargs: object) -> object:
        del argv, kwargs
        raise FileNotFoundError("missing")

    monkeypatch.setattr(mod.subprocess, "run", boom)  # type: ignore[attr-defined]
    outcome = mod._default_runner(["wslvar", "USERPROFILE"])
    assert outcome.returncode == 127
    assert outcome.stdout == ""
    assert "missing" in outcome.stderr


def test_default_runner_returns_127_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from muxdeck.adapters import windows_host as mod

    def boom(argv: object, /, **kwargs: object) -> object:
        del argv, kwargs
        raise subprocess.TimeoutExpired(cmd="x", timeout=4.0)

    monkeypatch.setattr(mod.subprocess, "run", boom)  # type: ignore[attr-defined]
    outcome = mod._default_runner(["cmd.exe"])
    assert outcome.returncode == 127


def test_default_runner_relays_subprocess_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from muxdeck.adapters import windows_host as mod

    captured: dict[str, object] = {}

    def fake_run(argv: object, /, **kwargs: object) -> object:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=argv,  # type: ignore[arg-type]
            returncode=0,
            stdout="hi",
            stderr="",
        )

    monkeypatch.setattr(mod.subprocess, "run", fake_run)  # type: ignore[attr-defined]
    outcome = mod._default_runner(["wslvar", "USERPROFILE"])
    assert outcome.returncode == 0
    assert outcome.stdout == "hi"
    assert captured["check"] is False
    assert captured["text"] is True
    assert captured["timeout"] == 4.0


# ── _userprofile_via_mnt_scan edge cases ─────────────────────────────


def test_mnt_scan_returns_none_when_users_dir_missing(tmp_path: Path) -> None:
    from muxdeck.adapters.windows_host import _userprofile_via_mnt_scan

    # tmp_path/Users does not exist → users_dir.is_dir() is False.
    assert _userprofile_via_mnt_scan(tmp_path) is None


def test_mnt_scan_skips_non_dirs_and_known_bot_users(tmp_path: Path) -> None:
    from muxdeck.adapters.windows_host import _userprofile_via_mnt_scan

    users_dir = tmp_path / "Users"
    users_dir.mkdir()
    # A non-directory entry must be skipped (line 156: "not entry.is_dir()").
    (users_dir / "rogue.txt").write_text("ignored")
    # A directory in the skip set must be skipped even if it has the
    # session-state subdir (line 156: "entry.name.lower() in skip").
    (users_dir / "Public" / ".copilot" / "session-state").mkdir(parents=True)
    # Real candidate that should be returned.
    (users_dir / "alice" / ".copilot" / "session-state").mkdir(parents=True)

    result = _userprofile_via_mnt_scan(tmp_path)
    assert result is not None
    winprofile, target = result
    assert winprofile == "C:\\Users\\alice"
    assert target == users_dir / "alice" / ".copilot" / "session-state"


# ── _try_resolver / _finalize edge cases ─────────────────────────────


def test_try_resolver_records_translation_failure(tmp_path: Path) -> None:
    # USERPROFILE is set but is unparseable (no drive letter), so
    # _winpath_to_wsl returns None and _try_resolver records an error
    # tagged env_userprofile (covers line 249).
    info = detect_windows_host(
        env={"USERPROFILE": "not-a-windows-path"},
        runner=lambda _argv: _fail(),
        osrelease_reader=lambda: "5.15-microsoft-WSL2",
        mnt_root=tmp_path / "missing",
    )
    assert info.is_wsl is True
    assert info.resolver == "env_userprofile"
    assert info.windows_userprofile == "not-a-windows-path"
    assert info.error is not None
    assert "could not translate" in info.error


def test_finalize_records_missing_session_state(tmp_path: Path) -> None:
    # Build a real /mnt/c/Users/dave but WITHOUT the .copilot dir, so
    # _finalize records "does not exist" rather than success.
    user_home = tmp_path / "Users" / "dave"
    user_home.mkdir(parents=True)

    def runner(argv: list[str]) -> CommandOutcome:
        if argv[0] == "wslvar":
            # Returning a path that translates to existing tmp_path/Users/dave
            # — but no .copilot/session-state under it.
            drive_letter = str(tmp_path).lstrip("/").split("/", maxsplit=1)[0]
            del drive_letter  # not used; we go through _winpath_to_wsl which assumes /mnt/X
            return _ok("C:\\Users\\dave\n")
        return _fail()

    # Translation maps to /mnt/c/Users/dave which won't exist; the
    # resolver records "does not exist" and we still end up at "none"
    # because subsequent fallbacks all fail too.
    info = detect_windows_host(
        env={"WSL_DISTRO_NAME": "Ubuntu"},
        runner=runner,
        osrelease_reader=lambda: "",
        mnt_root=tmp_path / "missing",
    )
    assert info.is_wsl is True
    # last_miss bubbles up — should be the wslvar candidate.
    assert info.resolver == "wslvar"
    assert info.error is not None
    assert info.session_state_dir is not None


def test_env_userprofile_returns_immediately_when_session_state_exists(
    tmp_path: Path,
) -> None:
    # Set up a real session-state directory under the simulated mnt root,
    # so _try_resolver returns an "available" candidate and detect_windows_host
    # short-circuits with resolver="env_userprofile" (covers lines 199-200).
    user_dir = tmp_path / "Users" / "alice"
    (user_dir / ".copilot" / "session-state").mkdir(parents=True)

    def runner(argv: list[str]) -> CommandOutcome:
        del argv
        return CommandOutcome(returncode=1, stdout="", stderr="")

    # _winpath_to_wsl translates "C:\\Users\\alice" → /mnt/c/Users/alice; we
    # want it instead to land on tmp_path. Point at tmp_path via mnt_root and
    # supply a userprofile string crafted so _winpath_to_wsl produces a path
    # under tmp_path. We can do that by patching the mnt resolution: simpler
    # to monkey-patch _winpath_to_wsl behavior is overkill — instead, we
    # create the same dir at /mnt/c if available; here we just verify the
    # branch via a custom env_userprofile that the translator accepts.
    def fake_translate(userprofile: str) -> Path | None:
        # All resolvers feeding _try_resolver get redirected to user_dir.
        return user_dir

    with patch.object(mod, "_winpath_to_wsl", new=fake_translate):
        info = mod.detect_windows_host(
            env={"WSL_DISTRO_NAME": "Ubuntu", "USERPROFILE": "C:\\Users\\alice"},
            runner=runner,
            osrelease_reader=lambda: "",
            mnt_root=tmp_path / "no-such-mnt",
        )
    # Short-circuit at env_userprofile branch.
    assert info.is_wsl is True
    assert info.resolver == "env_userprofile"
    assert info.error is None
    assert info.session_state_dir == user_dir / ".copilot" / "session-state"


def test_wslvar_and_cmd_exe_resolvers_short_circuit_when_available(
    tmp_path: Path,
) -> None:
    # No USERPROFILE in env → wslvar branch (lines 206-207) and cmd_exe
    # branch (lines 213-214) become reachable once the resolver returns
    # an "available" candidate. We simulate wslvar success → short circuit.
    user_dir = tmp_path / "Users" / "bob"
    (user_dir / ".copilot" / "session-state").mkdir(parents=True)

    def runner(argv: list[str]) -> CommandOutcome:
        if argv[0] == "wslvar":
            return CommandOutcome(returncode=0, stdout="C:\\Users\\bob", stderr="")
        return CommandOutcome(returncode=1, stdout="", stderr="")

    with patch.object(mod, "_winpath_to_wsl", new=lambda _: user_dir):
        info = mod.detect_windows_host(
            env={"WSL_DISTRO_NAME": "Ubuntu"},
            runner=runner,
            osrelease_reader=lambda: "",
            mnt_root=tmp_path / "no-such-mnt",
        )
    assert info.is_wsl is True
    assert info.resolver == "wslvar"
    assert info.error is None


def test_cmd_exe_resolver_short_circuits_when_wslvar_missing(
    tmp_path: Path,
) -> None:
    user_dir = tmp_path / "Users" / "carol"
    (user_dir / ".copilot" / "session-state").mkdir(parents=True)

    def runner(argv: list[str]) -> CommandOutcome:
        if argv[0] == "wslvar":
            return CommandOutcome(returncode=1, stdout="", stderr="not-found")
        if argv[0] == "cmd.exe":
            return CommandOutcome(
                returncode=0,
                stdout="C:\\Users\\carol",
                stderr="",
            )
        return CommandOutcome(returncode=1, stdout="", stderr="")

    with patch.object(mod, "_winpath_to_wsl", new=lambda _: user_dir):
        info = mod.detect_windows_host(
            env={"WSL_DISTRO_NAME": "Ubuntu"},
            runner=runner,
            osrelease_reader=lambda: "",
            mnt_root=tmp_path / "no-such-mnt",
        )
    assert info.is_wsl is True
    assert info.resolver == "cmd_exe"
    assert info.error is None


def test_finalize_returns_available_info_when_session_state_exists(
    tmp_path: Path,
) -> None:
    from muxdeck.adapters.windows_host import _finalize

    user_dir = tmp_path / "Users" / "dee"
    session_state = user_dir / ".copilot" / "session-state"
    session_state.mkdir(parents=True)
    info = _finalize(
        distro="Ubuntu",
        userprofile="C:\\Users\\dee",
        profile_path=user_dir,
        resolver="env_userprofile",
    )
    assert info.error is None
    assert info.session_state_dir == session_state


def test_userprofile_via_mnt_scan_skips_non_dirs(tmp_path: Path) -> None:
    from muxdeck.adapters.windows_host import _userprofile_via_mnt_scan

    users = tmp_path / "Users"
    users.mkdir()
    # File entries are skipped (line 156).
    (users / "stray-file.txt").write_text("x")
    # Skipped names are also skipped (line 156, "default" branch).
    (users / "Default").mkdir()
    # One legitimate entry — but no .copilot/session-state under it →
    # candidates stays empty → returns None.
    (users / "alice").mkdir()
    assert _userprofile_via_mnt_scan(tmp_path) is None
