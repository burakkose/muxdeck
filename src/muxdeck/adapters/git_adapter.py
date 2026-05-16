from __future__ import annotations

import functools
import re
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from muxdeck.domain.value_objects import CommandResult
from muxdeck.exceptions import CommandError, GitCommandError
from muxdeck.parsers import (
    AheadBehindCounts,
    GitStatusSummary,
    GitWorktreeRecord,
    parse_ahead_behind,
    parse_git_status_porcelain,
    parse_git_worktree_list_porcelain,
)
from muxdeck.types import CommandRunner, PathLike

_GIT_COMMON_DIR_ARGS = ("rev-parse", "--path-format=absolute", "--git-common-dir")
_GIT_TOPLEVEL_ARGS = ("rev-parse", "--path-format=absolute", "--show-toplevel")
_GIT_INSPECT_CONTEXT_ARGS = (
    "rev-parse",
    "--path-format=absolute",
    "--show-toplevel",
    "--git-common-dir",
    "--abbrev-ref",
    "HEAD",
)
_GIT_STATUS_ARGS = ("status", "--short", "--branch", "--untracked-files=all")
_GIT_WORKTREE_LIST_ARGS = ("worktree", "list", "--porcelain")
_GIT_LOG_FORMAT = "%h%x1f%cr%x1f%s"
_NO_UPSTREAM_SNIPPETS = (
    "no upstream configured",
    "no upstream branch",
    "no upstream information",
    "does not have any commits yet",
    "unknown revision",
    "head does not point to a branch",
)
_NO_COMMIT_HISTORY_SNIPPETS = (
    "does not have any commits yet",
    "bad default revision 'head'",
    "bad revision 'head'",
)


def _render_command(command: tuple[str, ...]) -> str:
    return shlex.join(command)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _translate_windows_drive_path(path: str) -> str | None:
    """Translate ``C:\\foo`` / ``C:/foo`` to ``/mnt/c/foo`` on POSIX hosts.

    Git porcelain output from a repo that lives on a Windows-mounted
    drive (e.g. WSL with ``/mnt/q`` ↔ Windows ``Q:``) can echo back
    drive-letter paths verbatim when worktrees were registered from
    the Windows side. ``Path("Q:/pm2").resolve()`` on POSIX silently
    joins the literal text onto the current working directory, which
    persists nonsense paths into the worktrees table and crashes any
    later ``git`` invocation.

    Returns the translated POSIX path string, or ``None`` if *path*
    does not look like a Windows drive-letter path.
    """
    if len(path) < 2 or path[1] != ":":
        return None
    drive = path[0]
    if not drive.isascii() or not drive.isalpha():
        return None
    if len(path) == 2:
        return f"/mnt/{drive.lower()}"
    separator = path[2]
    if separator not in ("/", "\\"):
        return None
    remainder = path[3:].replace("\\", "/").lstrip("/")
    return f"/mnt/{drive.lower()}/{remainder}" if remainder else f"/mnt/{drive.lower()}"


_WSL_MOUNT_RE = re.compile(r"^/mnt/([A-Za-z])(?:/(.*))?$")


def _wsl_path_to_windows(path: PathLike) -> str | None:
    """Translate ``/mnt/c/foo`` to ``C:\\foo`` (Windows-native).

    Worktrees registered from Windows (``git.exe``) stamp their
    ``.git/worktrees/<name>/gitdir`` records with backslash
    ``C:\\src\\Foo`` paths. WSL ``git worktree remove /mnt/c/src/Foo``
    cannot reconcile its POSIX argument against those records and
    refuses to operate. Routing the command through ``git.exe`` with
    a translated Windows path lets the record match exactly.

    Returns the Windows-native (single-backslash) representation when
    *path* is on a ``/mnt/<letter>/`` mount, or ``None`` otherwise so
    callers can fall back to the POSIX binary unchanged.
    """
    raw = str(path).replace("\\", "/")
    if raw != "/":
        raw = raw.rstrip("/")
    match = _WSL_MOUNT_RE.match(raw)
    if match is None:
        return None
    drive = match.group(1).upper()
    remainder = match.group(2) or ""
    if not remainder:
        return f"{drive}:" + chr(92)
    windows_remainder = remainder.replace("/", chr(92))
    return f"{drive}:" + chr(92) + windows_remainder


_WINDOWS_GIT_FALLBACK_PATHS: tuple[str, ...] = (
    "/mnt/c/Program Files/Git/cmd/git.exe",
    "/mnt/c/Program Files/Git/bin/git.exe",
    "/mnt/c/Program Files (x86)/Git/cmd/git.exe",
    "/mnt/c/Program Files (x86)/Git/bin/git.exe",
)


def _resolve_windows_git_binary(
    binary: str,
    *,
    fallback_paths: tuple[str, ...] = _WINDOWS_GIT_FALLBACK_PATHS,
    path_searcher: Callable[[str], str | None] = shutil.which,
    path_is_file: Callable[[str], bool] = lambda candidate: Path(candidate).is_file(),
) -> str | None:
    """Resolve ``binary`` (typically ``"git.exe"``) to an absolute path.

    Git for Windows is frequently installed but absent from the WSL
    ``PATH`` because the Windows ``PATH`` is not auto-propagated under
    every distro/interop configuration. Without resolution, calling
    ``subprocess.Popen(["git.exe", ...])`` from a WSL Python process
    fails with ``FileNotFoundError`` even though Git is present.

    Resolution order:

    1. If *binary* is already an absolute path, honour it verbatim —
       the operator passed it explicitly via the constructor so we
       must not silently swap it for a different install. Failures
       still surface naturally if the path is wrong.
    2. ``shutil.which(binary)`` — succeeds when the user has the
       Windows ``PATH`` propagated to WSL.
    3. A short list of standard Git for Windows install paths. The
       first existing file wins.

    Returns ``None`` when Git for Windows cannot be located, so
    callers can surface a single actionable error message instead of
    a generic ``FileNotFoundError``.
    """

    if Path(binary).is_absolute():
        return binary
    discovered = path_searcher(binary)
    if discovered is not None:
        return discovered
    for fallback in fallback_paths:
        if path_is_file(fallback):
            return fallback
    return None


def _is_windows_stamped_worktree(cwd: Path) -> bool:
    """Detect a linked worktree whose ``.git`` file references Windows paths.

    A linked worktree (anything except the main repo) on disk has a
    ``.git`` file (not directory) shaped like::

        gitdir: C:\\src\\foo\\.git\\worktrees\\name

    When that gitdir reference uses a Windows-native path (backslashes
    or a drive letter), WSL ``git`` cannot follow it and every command
    that touches the worktree (``rev-parse``, ``status``, ``worktree
    remove``) fails before the routed remove call can run. Detecting
    this stamp lets ``_run_command`` switch to ``git.exe`` for exactly
    these worktrees while leaving WSL-native ``/mnt`` repos
    (whose ``.git`` file references POSIX paths, or whose ``.git`` is
    a directory) untouched.
    """

    git_marker = cwd / ".git"
    try:
        if not git_marker.is_file():
            return False
        contents = git_marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        prefix, _, ref = line.partition(":")
        if prefix.strip().lower() != "gitdir":
            continue
        ref = ref.strip()
        if not ref:
            continue
        if "\\" in ref:
            return True
        if len(ref) >= 2 and ref[1] == ":" and ref[0].isascii() and ref[0].isalpha():
            return True
    return False


@functools.cache
def _detect_wsl_runtime() -> bool:
    """Best-effort WSL detection used when the runtime flag is unset.

    Mirrors the lightweight check already used in
    ``muxdeck.adapters.os_notifier``: inspect ``/proc/version`` for
    the ``microsoft``/``wsl`` markers. ``functools.cache`` keeps the
    file read off the per-call hot path; the boolean does not change
    over a process lifetime.
    """
    try:
        contents = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lowered = contents.lower()
    return "microsoft" in lowered or "wsl" in lowered


def _normalize_path(path: PathLike) -> Path:
    raw = str(path)
    translated = _translate_windows_drive_path(raw)
    if translated is not None:
        raw = translated
    return Path(raw).expanduser().resolve(strict=False)


def _path_contains(path: Path, candidate_root: Path) -> bool:
    return path == candidate_root or path.is_relative_to(candidate_root)


type GitSafetyCode = Literal[
    "ahead_of_upstream",
    "behind_upstream",
    "detached_head",
    "dirty_worktree",
    "locked_worktree",
    "merge_conflicts",
    "prunable_worktree",
]


@dataclass(frozen=True, slots=True)
class GitWorktreeInfo:
    repo_root: Path
    path: Path
    head_commit: str | None = None
    branch: str | None = None
    is_main_worktree: bool = False
    is_detached: bool = False
    is_bare: bool = False
    is_locked: bool = False
    lock_reason: str | None = None
    is_prunable: bool = False
    prunable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GitSafetyIssue:
    code: GitSafetyCode
    message: str
    worktree_path: Path | None = None


@dataclass(frozen=True, slots=True)
class GitRepositorySnapshot:
    repo_root: Path
    branch: str | None
    is_dirty: bool
    ahead_behind: AheadBehindCounts
    status_summary: GitStatusSummary
    current_worktree: GitWorktreeInfo | None
    safety_issues: tuple[GitSafetyIssue, ...]


@dataclass(frozen=True, slots=True)
class GitRepoContext:
    """Slim subset of repository state needed for runtime enrichment.

    Returned by :meth:`GitAdapter.inspect_repo_context`, which does
    the work of ``discover_repo_root`` + ``current_branch`` in a
    single ``git rev-parse`` invocation. Both fields preserve the
    pre-A2 semantics of those calls — see the adapter docstring.
    """

    repo_root: Path
    branch: str | None


@dataclass(frozen=True, slots=True)
class GitCommitSummary:
    short_sha: str
    relative_date: str
    subject: str


@dataclass(frozen=True, slots=True)
class GitWorktreeCreateRequest:
    path: PathLike
    branch: str | None = None
    start_point: str | None = None
    create_branch: bool = False
    force: bool = False
    detach: bool = False

    def __post_init__(self) -> None:
        normalized_path = _normalize_path(self.path)
        object.__setattr__(self, "path", normalized_path)
        object.__setattr__(self, "branch", _normalize_optional_text(self.branch))
        object.__setattr__(self, "start_point", _normalize_optional_text(self.start_point))
        if self.create_branch and self.branch is None:
            msg = "create_branch requires branch"
            raise ValueError(msg)
        if self.detach and self.create_branch:
            msg = "detach and create_branch cannot both be enabled"
            raise ValueError(msg)
        if self.detach and self.branch is not None:
            msg = "detach mode does not accept branch"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class GitWorktreeCreateOutcome:
    request: GitWorktreeCreateRequest
    worktree: GitWorktreeInfo
    command_result: CommandResult


@dataclass(frozen=True, slots=True)
class GitWorktreeRemoveOutcome:
    path: Path
    force: bool
    command_result: CommandResult


@dataclass(frozen=True, slots=True)
class GitWorktreePruneOutcome:
    dry_run: bool
    command_result: CommandResult
    worktrees: tuple[GitWorktreeInfo, ...]


class GitAdapter:
    def __init__(
        self,
        command_runner: CommandRunner,
        *,
        binary: str = "git",
        windows_binary: str = "git.exe",
        windows_binary_resolver: Callable[[str], str | None] | None = None,
        is_wsl_runtime: bool | None = None,
        timeout_sec: float = 10.0,
    ) -> None:
        if timeout_sec <= 0:
            msg = "timeout_sec must be greater than zero"
            raise ValueError(msg)
        self._command_runner = command_runner
        self._binary = binary
        self._windows_binary = windows_binary
        self._windows_binary_resolver: Callable[[str], str | None] = (
            windows_binary_resolver
            if windows_binary_resolver is not None
            else _resolve_windows_git_binary
        )
        self._resolved_windows_binary: str | None = None
        self._is_wsl_runtime = _detect_wsl_runtime() if is_wsl_runtime is None else is_wsl_runtime
        self._timeout_sec = timeout_sec
        self._windows_routing_cache: dict[Path, bool] = {}

    def _windows_git(self) -> str:
        """Return an absolute path to ``git.exe``, lazily resolving once.

        Raises :class:`GitCommandError` with an actionable message when
        Git for Windows cannot be found on PATH or at common install
        locations. This is the single seam where every
        Windows-routed call passes through, so the operator sees one
        clear error instead of an opaque ``FileNotFoundError`` from
        ``subprocess``.
        """

        if self._resolved_windows_binary is not None:
            return self._resolved_windows_binary
        resolved = self._windows_binary_resolver(self._windows_binary)
        if resolved is None:
            msg = (
                f"git for Windows ({self._windows_binary}) was not found on PATH "
                "or at standard install locations (e.g. C:\\Program Files\\Git). "
                "install Git for Windows or add git.exe to the WSL PATH so "
                "muxdeck can manage Windows-side worktrees."
            )
            raise GitCommandError(self._windows_binary, stderr=msg)
        self._resolved_windows_binary = resolved
        return resolved

    def _should_route_to_windows(self, cwd: Path) -> bool:
        """Decide whether ``_run_command`` must swap to ``git.exe`` for *cwd*.

        Routes only when the worktree at *cwd* is provably Windows-
        stamped (linked-worktree ``.git`` file references a native
        ``C:\\…`` / backslash path). Plain ``/mnt`` cwds without that
        evidence keep using the configured POSIX binary so existing
        WSL-native repos on Windows mounts are not regressed.
        """

        if not self._is_wsl_runtime:
            return False
        if _wsl_path_to_windows(cwd) is None:
            return False
        cached = self._windows_routing_cache.get(cwd)
        if cached is not None:
            return cached
        result = _is_windows_stamped_worktree(cwd)
        self._windows_routing_cache[cwd] = result
        return result

    def _select_worktree_binary_and_path(self, normalized_path: Path) -> tuple[str, str]:
        """Pick the git binary + path representation for *normalized_path*.

        Worktrees on a Windows drive (``/mnt/<letter>/...``) under WSL
        cannot be removed via WSL ``git``: the worktree records on
        disk were stamped by ``git.exe`` and contain native
        ``C:\\src\\Foo`` paths, so WSL git refuses to match the POSIX
        argument against them. Route those calls through ``git.exe``
        with a translated Windows path so the records line up.

        Outside WSL or for any non-mount path, return the configured
        POSIX binary and the path unchanged so existing behaviour is
        preserved. The cwd-based ``_run_command`` routing handles
        binary swapping + ``-C`` injection for commands without a
        path argv element; this helper is still needed for commands
        that DO take a worktree path as an argv element (e.g.
        ``worktree remove <path>``) so the path can be translated to
        the canonical Windows form ``git.exe`` records on disk.
        """
        if not self._is_wsl_runtime:
            return (self._binary, str(normalized_path))
        windows_path = _wsl_path_to_windows(normalized_path)
        if windows_path is None:
            return (self._binary, str(normalized_path))
        return (self._windows_binary, windows_path)

    def discover_repo_root(self, cwd: PathLike, /) -> Path:
        normalized_cwd = _normalize_path(cwd)
        worktree_root = self._discover_worktree_root(normalized_cwd)
        result = self._run_git(*_GIT_COMMON_DIR_ARGS, cwd=normalized_cwd)
        common_dir = _normalize_optional_text(result.stdout)
        if common_dir is None:
            self._raise_git_error(
                tuple(result.command),
                stderr="git returned an empty common directory",
                exit_code=result.exit_code,
                stdout=result.stdout,
            )
        common_dir_path = _normalize_path(common_dir)
        return common_dir_path.parent if common_dir_path.name == ".git" else worktree_root

    def current_branch(self, cwd: PathLike, /) -> str | None:
        result = self._run_git("branch", "--show-current", cwd=_normalize_path(cwd))
        return _normalize_optional_text(result.stdout)

    def inspect_repo_context(self, cwd: PathLike, /) -> GitRepoContext:
        """Return repo root and branch in a single ``git rev-parse`` call.

        Combines :meth:`discover_repo_root` and :meth:`current_branch`
        into one subprocess invocation by asking ``git rev-parse``
        for ``--show-toplevel`` + ``--git-common-dir`` + ``--abbrev-ref
        HEAD`` at the same time. The semantics match the two pre-A2
        calls exactly:

        * ``repo_root`` follows the same precedence as
          :meth:`discover_repo_root` — the common-dir parent wins
          when it is named ``.git`` (the normal worktree case),
          otherwise we fall back to the worktree top-level. Tests
          ``test_discover_repo_root_*`` codify this behaviour.
        * ``branch`` mirrors :meth:`current_branch`: ``None`` when
          ``HEAD`` is detached, otherwise the branch name. The
          ``--abbrev-ref HEAD`` form reports the literal ``HEAD``
          for detached heads, which is mapped back to ``None`` here
          so callers don't need to know about the difference.
        """
        normalized_cwd = _normalize_path(cwd)
        result = self._run_git(*_GIT_INSPECT_CONTEXT_ARGS, cwd=normalized_cwd)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) < 3:
            self._raise_git_error(
                tuple(result.command),
                stderr="git rev-parse returned fewer lines than expected",
                exit_code=result.exit_code,
                stdout=result.stdout,
            )
        worktree_root_text, common_dir_text, branch_text = lines[0], lines[1], lines[2]
        worktree_root = _normalize_path(worktree_root_text)
        common_dir_path = _normalize_path(common_dir_text)
        repo_root = common_dir_path.parent if common_dir_path.name == ".git" else worktree_root
        branch = None if branch_text == "HEAD" else _normalize_optional_text(branch_text)
        return GitRepoContext(repo_root=repo_root, branch=branch)

    def status(self, cwd: PathLike, /) -> GitStatusSummary:
        result = self._run_git(*_GIT_STATUS_ARGS, cwd=_normalize_path(cwd))
        return parse_git_status_porcelain(result.stdout)

    def is_dirty(self, cwd: PathLike, /) -> bool:
        return self.status(cwd).is_dirty

    def ahead_behind_counts(
        self,
        cwd: PathLike,
        /,
        *,
        status_summary: GitStatusSummary | None = None,
        branch: str | None = None,
    ) -> AheadBehindCounts:
        summary = status_summary if status_summary is not None else self.status(cwd)
        branch_counts = parse_ahead_behind(summary.branch_line or "")
        if branch_counts.recognized:
            return branch_counts
        effective_branch = branch if branch is not None else self.current_branch(cwd)
        if effective_branch is None:
            return AheadBehindCounts()
        try:
            result = self._run_git(
                "rev-list",
                "--left-right",
                "--count",
                "HEAD...@{upstream}",
                cwd=_normalize_path(cwd),
            )
        except GitCommandError as exc:
            stderr = (exc.stderr or "").casefold()
            if any(snippet in stderr for snippet in _NO_UPSTREAM_SNIPPETS):
                return AheadBehindCounts()
            raise
        return parse_ahead_behind(result.stdout)

    def list_worktrees(self, cwd: PathLike, /) -> tuple[GitWorktreeInfo, ...]:
        repo_root = self.discover_repo_root(cwd)
        return self._list_worktrees_from_repo_root(repo_root)

    def inspect_repository(self, cwd: PathLike, /) -> GitRepositorySnapshot:
        normalized_cwd = _normalize_path(cwd)
        repo_root = self.discover_repo_root(normalized_cwd)
        status_summary = self.status(normalized_cwd)
        worktrees = self._list_worktrees_from_repo_root(repo_root)
        branch = self.current_branch(normalized_cwd)
        ahead_behind = (
            AheadBehindCounts()
            if branch is None
            else self.ahead_behind_counts(
                normalized_cwd,
                status_summary=status_summary,
                branch=branch,
            )
        )
        current_worktree = self._find_current_worktree(normalized_cwd, worktrees)
        return GitRepositorySnapshot(
            repo_root=repo_root,
            branch=branch,
            is_dirty=status_summary.is_dirty,
            ahead_behind=ahead_behind,
            status_summary=status_summary,
            current_worktree=current_worktree,
            safety_issues=self._collect_safety_issues(
                repo_root=repo_root,
                cwd=normalized_cwd,
                current_worktree=current_worktree,
                status_summary=status_summary,
                ahead_behind=ahead_behind,
            ),
        )

    def list_recent_commits(
        self,
        cwd: PathLike,
        /,
        *,
        limit: int = 5,
    ) -> tuple[GitCommitSummary, ...]:
        if limit <= 0:
            return ()
        normalized_cwd = _normalize_path(cwd)
        command = (
            self._binary,
            "log",
            f"-{limit}",
            "--date=relative",
            f"--pretty=format:{_GIT_LOG_FORMAT}",
        )
        try:
            result = self._run_command(command, cwd=normalized_cwd)
        except GitCommandError as exc:
            stderr = (exc.stderr or "").casefold()
            if any(snippet in stderr for snippet in _NO_COMMIT_HISTORY_SNIPPETS):
                return ()
            raise
        commits: list[GitCommitSummary] = []
        for raw_line in result.stdout.splitlines():
            if not raw_line.strip():
                continue
            short_sha, separator, remainder = raw_line.partition("\x1f")
            relative_date, separator_two, subject = remainder.partition("\x1f")
            if not separator or not separator_two:
                self._raise_git_error(
                    command,
                    stderr=f"unexpected git log output: {raw_line}",
                    stdout=result.stdout,
                )
            commits.append(
                GitCommitSummary(
                    short_sha=short_sha.strip(),
                    relative_date=relative_date.strip(),
                    subject=subject.strip(),
                )
            )
        return tuple(commits)

    def build_create_worktree_command(
        self,
        request: GitWorktreeCreateRequest,
        /,
    ) -> tuple[str, ...]:
        args = [self._binary, "worktree", "add"]
        if request.force:
            args.append("--force")
        if request.detach:
            args.append("--detach")
        elif request.create_branch and request.branch is not None:
            args.extend(("-b", request.branch))
        args.append(str(request.path))
        if request.start_point is not None:
            args.append(request.start_point)
        elif request.branch is not None and not request.create_branch:
            args.append(request.branch)
        return tuple(args)

    def create_worktree(
        self,
        cwd: PathLike,
        request: GitWorktreeCreateRequest,
        /,
    ) -> GitWorktreeCreateOutcome:
        repo_root = self.discover_repo_root(cwd)
        worktrees = self._list_worktrees_from_repo_root(repo_root)
        command = self.build_create_worktree_command(request)
        self._ensure_create_request_safe(request, worktrees, command)
        result = self._run_command(command, cwd=repo_root)
        worktree = self._find_worktree_by_exact_path(
            request.path,
            self._list_worktrees_from_repo_root(repo_root),
        )
        if worktree is None:
            worktree = GitWorktreeInfo(
                repo_root=repo_root,
                path=_normalize_path(request.path),
                branch=request.branch,
                is_main_worktree=False,
                is_detached=request.detach,
            )
        return GitWorktreeCreateOutcome(request=request, worktree=worktree, command_result=result)

    def build_remove_worktree_command(
        self,
        path: PathLike,
        /,
        *,
        force: bool = False,
    ) -> tuple[str, ...]:
        binary, target = self._select_worktree_binary_and_path(_normalize_path(path))
        args = [binary, "worktree", "remove"]
        if force:
            args.append("--force")
        args.append(target)
        return tuple(args)

    def remove_worktree(
        self,
        path: PathLike,
        /,
        *,
        force: bool = False,
    ) -> GitWorktreeRemoveOutcome:
        normalized_path = _normalize_path(path)
        repo_root = self.discover_repo_root(normalized_path)
        worktrees = self._list_worktrees_from_repo_root(repo_root)
        command = self.build_remove_worktree_command(normalized_path, force=force)
        target_worktree = self._find_worktree_by_exact_path(normalized_path, worktrees)
        self._ensure_worktree_is_removable(target_worktree, command=command, force=force)
        if target_worktree is not None and not force:
            status_summary = self.status(target_worktree.path)
            if any(entry.is_unmerged for entry in status_summary.entries):
                self._raise_git_error(
                    command,
                    stderr=(
                        "worktree has unresolved merge conflicts; rerun with force=True to override"
                    ),
                )
            if status_summary.is_dirty:
                self._raise_git_error(
                    command,
                    stderr=("worktree has uncommitted changes; rerun with force=True to override"),
                )
        result = self._run_command(command, cwd=repo_root)
        return GitWorktreeRemoveOutcome(
            path=normalized_path,
            force=force,
            command_result=result,
        )

    def build_prune_worktrees_command(
        self,
        /,
        *,
        dry_run: bool = False,
        expire: str | None = None,
    ) -> tuple[str, ...]:
        args = [self._binary, "worktree", "prune"]
        if dry_run:
            args.append("--dry-run")
        normalized_expire = _normalize_optional_text(expire)
        if normalized_expire is not None:
            args.extend(("--expire", normalized_expire))
        return tuple(args)

    def prune_worktrees(
        self,
        cwd: PathLike,
        /,
        *,
        dry_run: bool = False,
        expire: str | None = None,
    ) -> GitWorktreePruneOutcome:
        repo_root = self.discover_repo_root(cwd)
        result = self._run_command(
            self.build_prune_worktrees_command(dry_run=dry_run, expire=expire),
            cwd=repo_root,
        )
        return GitWorktreePruneOutcome(
            dry_run=dry_run,
            command_result=result,
            worktrees=self._list_worktrees_from_repo_root(repo_root),
        )

    def _list_worktrees_from_repo_root(
        self,
        repo_root: Path,
    ) -> tuple[GitWorktreeInfo, ...]:
        result = self._run_git(*_GIT_WORKTREE_LIST_ARGS, cwd=repo_root)
        records = parse_git_worktree_list_porcelain(result.stdout)
        return tuple(self._record_to_worktree_info(repo_root, record) for record in records)

    def _record_to_worktree_info(
        self,
        repo_root: Path,
        record: GitWorktreeRecord,
    ) -> GitWorktreeInfo:
        path = _normalize_path(record.path)
        return GitWorktreeInfo(
            repo_root=repo_root,
            path=path,
            head_commit=record.head_commit,
            branch=record.branch,
            is_main_worktree=path == repo_root,
            is_detached=record.is_detached,
            is_bare=record.is_bare,
            is_locked=record.is_locked,
            lock_reason=record.lock_reason,
            is_prunable=record.is_prunable,
            prunable_reason=record.prunable_reason,
        )

    def _collect_safety_issues(
        self,
        *,
        repo_root: Path,
        cwd: Path,
        current_worktree: GitWorktreeInfo | None,
        status_summary: GitStatusSummary,
        ahead_behind: AheadBehindCounts,
    ) -> tuple[GitSafetyIssue, ...]:
        worktree_path = current_worktree.path if current_worktree is not None else cwd
        issues: list[GitSafetyIssue] = []
        if status_summary.is_dirty:
            issues.append(
                GitSafetyIssue(
                    code="dirty_worktree",
                    message="worktree has uncommitted changes",
                    worktree_path=worktree_path,
                )
            )
        if any(entry.is_unmerged for entry in status_summary.entries):
            issues.append(
                GitSafetyIssue(
                    code="merge_conflicts",
                    message="worktree has unresolved merge conflicts",
                    worktree_path=worktree_path,
                )
            )
        if current_worktree is not None and current_worktree.is_detached:
            issues.append(
                GitSafetyIssue(
                    code="detached_head",
                    message="current worktree is in detached HEAD state",
                    worktree_path=current_worktree.path,
                )
            )
        if current_worktree is not None and current_worktree.is_locked:
            issues.append(
                GitSafetyIssue(
                    code="locked_worktree",
                    message=current_worktree.lock_reason or "worktree is locked",
                    worktree_path=current_worktree.path,
                )
            )
        if current_worktree is not None and current_worktree.is_prunable:
            issues.append(
                GitSafetyIssue(
                    code="prunable_worktree",
                    message=current_worktree.prunable_reason or "worktree is prunable",
                    worktree_path=current_worktree.path,
                )
            )
        if ahead_behind.ahead > 0:
            issues.append(
                GitSafetyIssue(
                    code="ahead_of_upstream",
                    message=f"branch is ahead of upstream by {ahead_behind.ahead}",
                    worktree_path=worktree_path if current_worktree is not None else repo_root,
                )
            )
        if ahead_behind.behind > 0:
            issues.append(
                GitSafetyIssue(
                    code="behind_upstream",
                    message=f"branch is behind upstream by {ahead_behind.behind}",
                    worktree_path=worktree_path if current_worktree is not None else repo_root,
                )
            )
        return tuple(issues)

    def _ensure_create_request_safe(
        self,
        request: GitWorktreeCreateRequest,
        worktrees: tuple[GitWorktreeInfo, ...],
        command: tuple[str, ...],
    ) -> None:
        normalized_path = _normalize_path(request.path)
        existing_worktree = self._find_worktree_by_exact_path(normalized_path, worktrees)
        if existing_worktree is not None and not request.force:
            self._raise_git_error(
                command,
                stderr=(
                    f"worktree path already exists: {normalized_path}; rerun with force=True "
                    "to override"
                ),
            )
        if request.branch is None or request.detach or request.force:
            return
        conflicting_worktree = next(
            (
                worktree
                for worktree in worktrees
                if worktree.branch == request.branch and worktree.path != normalized_path
            ),
            None,
        )
        if conflicting_worktree is not None:
            self._raise_git_error(
                command,
                stderr=(
                    "branch is already checked out in another worktree: "
                    f"{conflicting_worktree.path}; rerun with force=True to override"
                ),
            )

    def _ensure_worktree_is_removable(
        self,
        worktree: GitWorktreeInfo | None,
        *,
        command: tuple[str, ...],
        force: bool,
    ) -> None:
        if worktree is None:
            self._raise_git_error(command, stderr="worktree path is not registered")
        if worktree.is_main_worktree:
            self._raise_git_error(command, stderr="refusing to remove the main worktree")
        if worktree.is_locked and not force:
            self._raise_git_error(
                command,
                stderr=(
                    f"{worktree.lock_reason or 'worktree is locked'}; rerun with force=True "
                    "to override"
                ),
            )

    def _find_current_worktree(
        self,
        cwd: Path,
        worktrees: tuple[GitWorktreeInfo, ...],
    ) -> GitWorktreeInfo | None:
        matching = [worktree for worktree in worktrees if _path_contains(cwd, worktree.path)]
        if not matching:
            return None
        return max(matching, key=lambda worktree: len(str(worktree.path)))

    def _find_worktree_by_exact_path(
        self,
        path: PathLike,
        worktrees: tuple[GitWorktreeInfo, ...],
    ) -> GitWorktreeInfo | None:
        normalized_path = _normalize_path(path)
        for worktree in worktrees:
            if worktree.path == normalized_path:
                return worktree
        return None

    def _discover_worktree_root(self, cwd: Path) -> Path:
        result = self._run_git(*_GIT_TOPLEVEL_ARGS, cwd=cwd)
        worktree_root = _normalize_optional_text(result.stdout)
        if worktree_root is None:
            self._raise_git_error(
                tuple(result.command),
                stderr="git returned an empty worktree root",
                exit_code=result.exit_code,
                stdout=result.stdout,
            )
        return _normalize_path(worktree_root)

    def _run_git(self, *args: str, cwd: Path) -> CommandResult:
        return self._run_command((self._binary, *args), cwd=cwd)

    def _run_command(self, command: tuple[str, ...], *, cwd: Path) -> CommandResult:
        routed_command = self._route_command_for_cwd(command, cwd)
        try:
            result = self._command_runner.run(
                routed_command,
                cwd=cwd,
                timeout_sec=self._timeout_sec,
            )
        except CommandError as exc:
            raise GitCommandError(
                exc.command,
                exit_code=exc.exit_code,
                stderr=exc.stderr,
                stdout=exc.stdout,
            ) from exc
        if result.succeeded:
            return result
        return self._raise_git_error(
            tuple(result.command),
            stderr=result.stderr,
            exit_code=result.exit_code,
            stdout=result.stdout,
        )

    def _route_command_for_cwd(
        self,
        command: tuple[str, ...],
        cwd: Path,
    ) -> tuple[str, ...]:
        """Route git invocations through ``git.exe`` with explicit Windows cwd.

        Two distinct cases land here:

        1. ``command[0] == self._binary`` (POSIX git) and *cwd* is a
           Windows-stamped worktree — swap the binary to the resolved
           ``git.exe`` absolute path.
        2. ``command[0]`` is already ``git.exe`` (built by
           :meth:`_select_worktree_binary_and_path` for commands that
           carry a worktree path as argv) — keep it but normalize to
           the resolved absolute path.

        In both cases we ALSO inject ``-C <windows_cwd>`` so ``git.exe``
        sees a Windows-native working directory instead of relying on
        WSL interop's implicit translation of the POSIX cwd passed to
        ``subprocess``. This makes path handling deterministic — every
        path ``git.exe`` looks at, including its own working directory,
        is unambiguously Windows-shaped.

        ``-C`` is skipped when *cwd* doesn't translate to a Windows
        path (non-``/mnt`` cwds for absolute git.exe configs the user
        forced explicitly) or when the command already starts with an
        explicit ``-C`` so we never double-inject.

        The subprocess ``cwd`` is left as the POSIX path by the
        caller: that keeps a valid working dir for the WSL process
        itself (useful for error messages and any tooling that reads
        ``/proc/<pid>/cwd``) while ``git -C`` overrides where ``git``
        operates.
        """

        if not command:
            return command

        binary = command[0]
        is_windows_binary = binary == self._windows_binary or (
            binary != self._binary and Path(binary).name.lower() == "git.exe"
        )
        needs_swap = binary == self._binary and self._should_route_to_windows(cwd)
        if not is_windows_binary and not needs_swap:
            return command

        resolved_binary = self._windows_git()
        rest = command[1:]
        has_dash_c = len(rest) >= 2 and rest[0] == "-C"
        if has_dash_c:
            return (resolved_binary, *rest)
        windows_cwd = _wsl_path_to_windows(cwd)
        if windows_cwd is None:
            return (resolved_binary, *rest)
        return (resolved_binary, "-C", windows_cwd, *rest)

    def _raise_git_error(
        self,
        command: tuple[str, ...],
        *,
        stderr: str | None,
        exit_code: int | None = None,
        stdout: str | None = None,
    ) -> NoReturn:
        raise GitCommandError(
            _render_command(command),
            exit_code=exit_code,
            stderr=stderr,
            stdout=stdout,
        )
