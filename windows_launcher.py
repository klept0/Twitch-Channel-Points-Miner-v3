# -*- coding: utf-8 -*-

"""Windows executable entry point for Twitch Channel Points Miner."""

import base64
import csv
import ctypes  # cross-platform stdlib module; only .windll is Windows-only (guarded below)
import getpass
import importlib.util
import json
import re
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)


class _NullStream:
    """Absorbs writes harmlessly.

    A --windowed PyInstaller build starts with sys.stdout/sys.stderr set to
    None (no OS console attached) until something replaces them. Without
    this, the first print() or log call anywhere in this process - including
    ones triggered merely by importing TwitchChannelPointsMiner below, before
    main() ever runs - would crash with an AttributeError on a None stream.
    main() replaces this with the real Console-tab capture almost
    immediately; anything written before then is lost, not shown in the
    Console tab, but the process no longer crashes over it.
    """

    def write(self, _text):
        pass

    def flush(self):
        pass

    def isatty(self):
        return False


if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()


from TwitchChannelPointsMiner import __version__  # noqa: E402
from TwitchChannelPointsMiner.classes.AnalyticsServer import (  # noqa: E402
    BUILD_COMMIT_ENV_VAR,
    SHELL_BYPASS_TOKEN_ENV_VAR,
)
from TwitchChannelPointsMiner.config_editor import (
    ConfigEditError,
    enable_analytics_dashboard,
    read_config_literal,
)
from TwitchChannelPointsMiner.runner import main as runner_main  # noqa: E402

DEFAULT_ANALYTICS_PORT = 54455
# Deliberately not 5000: that port is commonly already taken (Hyper-V's
# reserved dynamic port ranges on Windows, other dev tools, or a leftover
# copy of this app itself), which surfaces as a confusing bind failure or,
# worse, the shell silently talking to whatever else is already there. This
# high, uncommon port is only the default for a *newly created* config -
# users can still change it in ANALYTICS_CONFIG at any time, and an existing
# config that already has a 'port' value keeps it untouched.
_PLACEHOLDER_USERNAME = "your-twitch-username"
_TRAY_ICON_FILE = "twitch-miner.ico"
_AUTOSTART_VALUE_NAME = "TwitchChannelPointsMiner"
_AUTOSTART_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_START_MINIMIZED_FLAG = "--start-minimized"
_SHELL_PREFS_FILENAME = "shell_prefs.json"
_CLOSE_BEHAVIOR_TRAY = "tray"
_CLOSE_BEHAVIOR_QUIT = "quit"


def _mutex_user_suffix():
    """The signed-in account, made safe for a kernel object name.

    Background mining started at boot runs in session 0, where a "Local\\"
    name is invisible to the desktop session, so the instance mutexes are
    "Global\\". The account in the name keeps two Windows users on one PC
    from blocking each other, as the per-session name used to.
    """
    domain = os.environ.get("USERDOMAIN", "")
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME", "")
    raw = f"{domain}.{user}" if domain else user
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in raw) or "user"


# Global, per account: see _mutex_user_suffix. Creating a Global mutex needs
# no elevated privilege (only Global file mappings do).
_SINGLE_INSTANCE_MUTEX_NAME = (
    "Global\\TwitchChannelPointsMinerSingleInstance-" + _mutex_user_suffix()
)
# Loopback-only "please show yourself" signal for a second launch to send
# the first. Distinct from the analytics dashboard's own (configurable,
# default 54455) port.
_SINGLE_INSTANCE_HOST = "127.0.0.1"
_SINGLE_INSTANCE_PORT = 54876
# Launcher-only flags for background mode (installed app only). They are
# handled before anything is forwarded, so they never reach runner_main().
_BACKGROUND_FLAG = "--background"
_BACKGROUND_WORKER_FLAG = "--background-worker"
_STOP_BACKGROUND_FLAG = "--stop-background"
_REGISTER_BACKGROUND_FLAG = "--register-background"
_UNREGISTER_BACKGROUND_FLAG = "--unregister-background"
# Starts background mining now: through its scheduled task when there is one,
# so a boot task keeps running after sign-out, otherwise as a child process.
_START_BACKGROUND_FLAG = "--start-background"
# Shows whether background mining runs, how it starts and where its log is.
_BACKGROUND_STATUS_FLAG = "--background-status"
# The background (headless) build's first-run console: Twitch username and
# sign-in, then background mining starts. The default for that build.
_SETUP_FLAG = "--setup"
# With --register-background: also start at boot, before anyone signs in.
_AT_BOOT_FLAG = "--at-boot"
# A second named mutex, held only by the background supervisor, so a desktop
# launch can tell "background mining is running" apart from "the desktop
# window is running" - both are otherwise the same single-instance lock.
_BACKGROUND_MUTEX_NAME = _SINGLE_INSTANCE_MUTEX_NAME + "-background"
# Written by --stop-background (and the unregister flow) and consumed by the
# supervisor or worker that sees it first; its presence is the stop request.
_BACKGROUND_STOP_FILE = "background.stop"
_BACKGROUND_TASK_NAME = "Twitch Channel Points Miner (background)"
_BACKGROUND_RUN_VALUE_NAME = "TwitchChannelPointsMinerBackground"
# Exit code the miner uses to ask for a restart (runner.restart_process, via
# TCPM_RESTART_EXIT_CODE). Any other nonzero worker exit counts as a crash.
_BACKGROUND_RESTART_EXIT_CODE = 3
_BACKGROUND_BACKOFF_START = 30
_BACKGROUND_BACKOFF_MAX = 600
# A worker that ran this long before exiting was healthy, so its exit does not
# continue an older crash streak - the backoff starts over.
_BACKGROUND_HEALTHY_RUN_SECONDS = 3600
_BACKGROUND_POLL_SECONDS = 1
_BACKGROUND_STOP_TIMEOUT = 300
_BACKGROUND_LOG_MAX_BYTES = 1024 * 1024
_BACKGROUND_WORKER_LOG_MAX_BYTES = 5 * 1024 * 1024
_CREATE_NO_WINDOW = 0x08000000
_SYNCHRONIZE = 0x00100000
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
# Indirections so tests can drive the supervisor loop without real waiting.
_background_sleep = time.sleep
_background_clock = time.monotonic
# Set only inside the background worker process. A windowed exe has no desktop
# to show a message box on, and a modal box would hang an unattended worker
# forever, so _message_box logs to stderr instead while this is set.
_headless_worker = False
# The single-instance mutex handle, kept so --setup can let go of it before
# it starts background mining; every other caller holds it until exit.
_single_instance_handle = None
# Job-object handles must stay referenced for the parent's whole lifetime:
# closing the last handle is what makes the kill-on-close limit fire.
_background_job_handles = []


def application_directory():
    """Return the user-owned directory containing the executable or script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_file(name):
    """Return a file bundled by PyInstaller or present in the source checkout."""
    bundle_directory = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_directory / name


def _build_install_mode():
    """Returns "standard" or "portable", baked into the bundle at build time
    by build_windows.bat's two separate invocations - not inferred from the
    exe's own filename (trivially renamed by the end user, which would
    otherwise silently flip behavior) or anything else decided after the
    fact. Missing or unreadable (always true for a source checkout, which
    has no bundle at all) defaults to "portable": the long-standing, safe
    behavior of keeping everything beside the script/exe.
    """
    try:
        return bundled_file("install_mode.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "portable"


def _is_standard_build():
    return _build_install_mode() == "standard"


def _build_flavor():
    """Returns "headless" for the background build, which leaves out the
    desktop window and tray, or "desktop" for every other build. Baked in by
    build_windows.bat the same way as install_mode.txt."""
    try:
        return bundled_file("build_flavor.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "desktop"


def _is_headless_build():
    return _build_flavor() == "headless"


def _build_commit_hash():
    """Short commit hash this build was made from, baked in by
    build_windows.bat the same way install_mode.txt is - or None for a
    source checkout (no bundle to read from at all) or a build that
    couldn't determine one (e.g. no git available at build time), which
    build_windows.bat already writes as the literal string "unknown" for
    exactly this case.
    """
    try:
        commit = bundled_file("commit_hash.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return commit if commit and commit != "unknown" else None


def _standard_application_directory():
    """Per-user Windows data directory for a "standard" build - deliberately
    separate from wherever the exe binary itself lives, so installing,
    upgrading, or uninstalling the app never touches configuration, cookies,
    analytics, or logs."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "TwitchChannelPointsMiner"


def config_root_directory(exe_dir):
    """Where config/cookies/analytics/logs live for this run."""
    if _is_standard_build():
        return _standard_application_directory()
    return exe_dir


def prepare_config(application_dir):
    """Create the external configuration template on first launch."""
    config_dir = application_dir / "config"
    config_path = config_dir / "config.py"
    if config_path.is_file():
        return config_dir, False

    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bundled_file("config.example.py"), config_path)
    try:
        config_path.chmod(0o600)
    except OSError:
        # Windows and some shared filesystems do not support POSIX permissions.
        pass
    return config_dir, True


def _prepare_application_directories():
    """Resolve where this run keeps its data, create it, and make it the
    working directory. Shared by main() and the background-mode entry points.

    For a standard build this may be a brand-new per-user AppData folder
    that has never existed before (nothing else creates it up front, as
    exe_dir always trivially exists already) - os.chdir() requires it to
    exist first.
    """
    exe_dir = application_directory()
    application_dir = config_root_directory(exe_dir)
    application_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(application_dir)
    return exe_dir, application_dir


def pause_for_first_run():
    """Keep a Windows console visible after creating its initial config, or
    after the desktop shell could not be opened."""
    if os.name != "nt":
        return
    if sys.stdin is None:
        # A --windowed/--noconsole build has no console and thus no stdin
        # at all; input() raises RuntimeError("lost sys.stdin") immediately
        # rather than EOFError in this case, so it must be checked first.
        return
    try:
        input("Press Enter to close this window...")
    except EOFError:
        # A redirected or otherwise non-interactive launch may still have
        # a stdin object that simply has nothing to read.
        pass


def _message_box(text, title, icon_flag):
    """Best-effort native message box for when nothing else can reach the
    user - a --windowed build has no console, and this may run before the
    desktop shell window exists (or after it failed to open) either way.
    """
    if _headless_worker:
        # See _headless_worker: nothing can dismiss a box in an unattended run.
        print(f"{title}: {text}", file=sys.stderr, flush=True)
        return
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, icon_flag)
    except Exception:
        pass


def _show_fatal_error_message(text):
    _message_box(text, "Twitch Channel Points Miner - Error", 0x10)  # MB_ICONERROR


def _show_migration_notice(text):
    _message_box(text, "Twitch Channel Points Miner", 0x40)  # MB_ICONINFORMATION


# Carried forward into the new standard location - the app needs these to
# keep working exactly where the user left off (same account, cookies,
# watch/points history).
_LEGACY_DIRS_CARRY_FORWARD = ("config", "cookies", "analytics")
# Archived only, never copied forward: old logs aren't part of continuing to
# run and are never rotated/cleaned by anything once they're just sitting in
# a copy - carrying them into the live logs folder would mean an old,
# unbounded pile the app's normal log management never touches.
_LEGACY_DIRS_ARCHIVE_ONLY = ("logs",)


def _migrate_legacy_windows_data(exe_dir, standard_dir):
    """One-time migration for a "standard" build that finds data from before
    this feature existed, back when everything lived beside the exe (the
    only behavior a portable build, or any older version, has ever had).

    Moves each old data folder into <exe_dir>/config-legacy/<name> (so
    nothing is lost regardless of what happens next), then copies the ones
    the app actually needs going forward into the new standard location -
    old logs are archived but deliberately not carried into the live logs
    folder (see _LEGACY_DIRS_ARCHIVE_ONLY).

    Gated on a marker *inside* the archive folder (config-legacy/.migrated),
    not on whether a legacy config.py still exists - once archived,
    config.py no longer exists at its old path at all, so re-checking that
    on a later run would look identical to "nothing to migrate", and this
    must not run twice regardless (a second run would archive the fresh
    standard-location data instead of the real legacy data). For the same
    reason, "is there anything to do" also can't be answered by checking for
    just config.py: an earlier attempt can fail partway through (e.g.
    "config" moved, then "cookies" hits a locked file) without ever writing
    the marker, at which point config.py is already gone from its old path
    even though cookies/analytics/logs are still stranded there - so this
    also treats a config-legacy folder that exists without a .migrated
    marker as an interrupted migration to resume, regardless of what's left
    at the old path.

    Whether the copy-forward step for a given item has already run is
    tracked with its own marker inside legacy_root (config-legacy/.<name>
    -copied), not by checking whether `destination` exists: prepare_config()
    can create <standard_dir>/config/config.py on an earlier, ordinary
    launch that happens to run before this legacy data was ever discovered
    (e.g. the user installed the "standard" build and ran it once before
    copying their old portable folder's contents alongside the exe) -
    "destination already exists" is then true for a reason that has nothing
    to do with this migration ever having copied anything, and skipping the
    copy on that basis would silently strand the user's real config behind
    a bootstrap template that looks, from here, indistinguishable from a
    completed migration. The copy itself uses dirs_exist_ok=True so it
    still succeeds (overwriting any such stale content with the real
    archived data) regardless of what, if anything, already sits at
    `destination`.

    Returns None if there was nothing to do, or a message describing what
    happened (success or partial failure) for a one-time notice to the user.
    """
    legacy_root = exe_dir / "config-legacy"
    if (legacy_root / ".migrated").is_file():
        return None
    has_legacy_source = any(
        (exe_dir / name).is_dir()
        for name in _LEGACY_DIRS_CARRY_FORWARD + _LEGACY_DIRS_ARCHIVE_ONLY
    )
    if not has_legacy_source and not legacy_root.exists():
        return None  # nothing to migrate - a fresh standard install

    try:
        legacy_root.mkdir(parents=True, exist_ok=True)
        for name in _LEGACY_DIRS_CARRY_FORWARD + _LEGACY_DIRS_ARCHIVE_ONLY:
            source = exe_dir / name
            archived = legacy_root / name
            # `source` may already be gone even on a first pass through this
            # item within a resumed migration (see the docstring above) - the
            # carry-forward copy below must still run off `archived` in that
            # case, not be skipped just because there's nothing left to move.
            if source.is_dir() and not archived.exists():
                shutil.move(str(source), str(archived))
            if name in _LEGACY_DIRS_CARRY_FORWARD:
                destination = standard_dir / name
                copied_marker = legacy_root / f".{name}-copied"
                if archived.is_dir() and not copied_marker.is_file():
                    shutil.copytree(archived, destination, dirs_exist_ok=True)
                    copied_marker.touch()
        (legacy_root / ".migrated").touch()
    except OSError as error:
        return (
            "Could not fully move your existing configuration to its new "
            f"location ({error}).\n\nYour original files are safe at:\n"
            f"{legacy_root}"
        )

    return (
        "Your configuration, cookies, and analytics were moved to:\n"
        f"{standard_dir}\n\n"
        "Your original files, including past logs, were kept, untouched, "
        f"at:\n{legacy_root}"
    )


def _matches_template_defaults(source):
    """True only if this config's CURRENT VALUES for enable_analytics and
    ANALYTICS_CONFIG equal the bundled template's defaults (False and None).

    This confirms the config currently matches those defaults - it does NOT
    confirm the file was never intentionally set that way; those are
    different guarantees. A user who deliberately chose
    enable_analytics=False and left ANALYTICS_CONFIG unset produces content
    indistinguishable from an untouched template. This is purely a
    defense-in-depth check (for a bundled template whose shape has changed
    unexpectedly), layered on top of the provenance check in
    ensure_windows_analytics_defaults() - it is not, by itself, a safe
    substitute for that check. Parsed with config_editor.py's existing
    public literal reader rather than a second, parallel implementation.
    """

    try:
        missing = object()
        return (
            read_config_literal(source, "MINER_CONFIG", "enable_analytics") is False
            and read_config_literal(source, "ANALYTICS_CONFIG", default=missing) is None
        )
    except SyntaxError:
        return False


def _needs_username(config_path):
    """True if MINER_CONFIG['username'] is still the bundled template's
    placeholder, or blank.

    Safe to check by content, unlike the analytics gating above: a Twitch
    username can't contain a hyphen, so "your-twitch-username" can never be
    a value a real user deliberately chose - there is no legitimate history
    that looks identical to this one, the way enable_analytics=False can
    hide either an untouched template or a deliberate choice.
    """
    try:
        username = read_config_literal(
            config_path.read_text(encoding="utf-8"), "MINER_CONFIG", "username"
        )
    except (OSError, SyntaxError):
        return False
    return (
        not isinstance(username, str)
        or not username.strip()
        or username == _PLACEHOLDER_USERNAME
    )


def ensure_windows_analytics_defaults(config_path, just_created):
    """Turn on the embedded dashboard with a generated password.

    The bundled template ships with analytics disabled, so a brand-new user
    would otherwise have nothing for the shell's Dashboard tab to show.

    `just_created` must be True only when the caller's own copy of the
    bundled template to `config_path` happened in *this* run (see
    `prepare_config`'s return value). This function deliberately does not,
    and cannot safely, infer freshness by reopening and inspecting the
    file's current content: a user who deliberately set
    enable_analytics=False and left ANALYTICS_CONFIG unset - a legitimate,
    intentional choice - produces content byte-for-byte indistinguishable
    from an untouched template. Only provenance (did *this* call just create
    the file?) can tell those two histories apart; a content check cannot,
    no matter how it is implemented. Making the caller pass this explicitly
    turns "don't call this on an existing config" into an API contract a
    future refactor can see and violate visibly, instead of a content
    heuristic it could silently defeat.

    A secondary, defense-in-depth content check (`_matches_template_defaults`)
    still runs after the provenance check passes, in case the bundled
    template's own shape has changed unexpectedly - see its docstring for
    why it is not a substitute for the provenance check above.

    windows_installer.iss's CustomizeStarterConfig is a Pascal port of this
    for the installer's own pre-created config.py, correctly gated instead
    by Inno Setup's onlyifdoesntexist/AfterInstall provenance (a file-copy
    that is skipped never runs AfterInstall) - keep the two in sync if the
    written defaults change.
    """
    if not just_created:
        return None

    source = config_path.read_text(encoding="utf-8")
    if not _matches_template_defaults(source):
        return None

    password = secrets.token_urlsafe(18)
    try:
        # Same AST-based writer the shell's "enable the dashboard?" prompt
        # and Settings tab use for an existing config, rather than a second,
        # parallel text-patching implementation of the same ANALYTICS_CONFIG
        # shape (see enable_analytics_dashboard's docstring).
        enable_analytics_dashboard(config_path, password, port=DEFAULT_ANALYTICS_PORT)
    except (ConfigEditError, OSError, SyntaxError):
        # _matches_template_defaults confirmed enable_analytics is False and
        # ANALYTICS_CONFIG is unset, but the template's exact shape wasn't
        # what the AST writer expected (e.g. it changed) - bail out rather
        # than leave a half-written config.
        return None
    return password


def resolve_dashboard_info(config_path):
    """Best-effort peek at the analytics settings for the shell's Dashboard tab.

    Returns None when analytics is disabled (or the config cannot be parsed
    yet), so the shell can show a helpful message instead of a dead iframe;
    the Console tab still shows the real reason via the miner's own startup
    logging.
    """
    try:
        from TwitchChannelPointsMiner.runner import _load_config

        config = _load_config(config_path)
    except Exception:
        return None

    if config.MINER_CONFIG.get("enable_analytics") is not True:
        return None
    analytics_config = config.ANALYTICS_CONFIG
    if not isinstance(analytics_config, dict):
        return None

    host = analytics_config.get("host", "127.0.0.1")
    port = analytics_config.get("port", DEFAULT_ANALYTICS_PORT)
    # 0.0.0.0 (or similar) is a bind address, not something the embedded
    # window can connect to - view the locally-running server via loopback.
    display_host = host if host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    return {
        "host": display_host,
        "port": port,
        "url": f"http://{display_host}:{port}/",
    }


class ConsoleBuffer:
    """Bounded, thread-safe ring buffer of console output for the shell's
    Console tab.

    Captures raw stdout/stderr writes rather than hooking into `logging`
    directly, so it also shows tracebacks and any output that never goes
    through a logger - the only requirement for something to reach the
    embedded Console tab in a windowed (console-less) build where stdout
    would otherwise go nowhere.
    """

    def __init__(self, max_entries=4000):
        self._entries = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._seq = 0

    def write(self, text):
        if not text:
            return
        with self._lock:
            self._seq += 1
            self._entries.append((self._seq, text))

    def tail(self, since_seq=0, max_entries=500):
        with self._lock:
            entries = [entry for entry in self._entries if entry[0] > since_seq]
        if len(entries) > max_entries:
            entries = entries[-max_entries:]
        next_seq = entries[-1][0] if entries else since_seq
        return [text for _seq, text in entries], next_seq


class _TeeStream:
    """Duplicates writes to a ConsoleBuffer and, if present, the real stream."""

    def __init__(self, buffer, underlying):
        self._buffer = buffer
        self._underlying = underlying

    def write(self, text):
        self._buffer.write(text)
        if self._underlying is not None:
            try:
                self._underlying.write(text)
            except (OSError, ValueError):
                self._underlying = None

    def flush(self):
        if self._underlying is not None:
            try:
                self._underlying.flush()
            except (OSError, ValueError):
                pass

    def isatty(self):
        return False

    # Report the forwarded stream's codec: a write it cannot encode makes
    # write() drop that stream for good, so callers need to know about it.
    @property
    def encoding(self):
        return getattr(self._underlying, "encoding", None)


def install_console_capture(buffer):
    """Mirror stdout/stderr into `buffer` for the shell's Console tab.

    Installed as early as possible in main() so it captures startup prints,
    the logging module's console handler (configured later, once the miner
    thread reaches Settings.logger setup), and any traceback a windowed
    build (no OS console) would otherwise lose entirely. Whatever is
    currently installed - a real stream, or the module-level _NullStream
    fallback above - becomes this tee's underlying stream, so nothing
    printed before this call is duplicated once it runs.
    """
    sys.stdout = _TeeStream(buffer, sys.stdout)
    sys.stderr = _TeeStream(buffer, sys.stderr)


def _configure_launcher_logging():
    """Make this module's own `logger.info(...)` calls (the shell-lifecycle
    diagnostics in launch_shell/_make_hide_to_tray_handler) actually show up
    in the Console tab from the moment the window exists.

    Without this, `logger` has no handler and sits at the logging module's
    default WARNING level until - if ever - the miner thread reaches
    TwitchChannelPointsMiner.__init__ and calls configure_loggers(), which
    only happens once mining actually starts. That's arbitrarily late (never,
    on first run, until a username is submitted; otherwise racing window
    creation) - exactly when a user is most likely to be poking at
    minimize/restore/close, so those log lines would otherwise be silently
    dropped rather than merely delayed.

    Attaches directly to this module's own logger (not the root logger) and
    disables propagation, so this stays independent of - and never
    double-prints alongside - the miner's own, separately-configured
    category/emoji-aware console formatting once configure_loggers() does
    eventually run.

    Called after install_console_capture() so `sys.stdout` here is already
    the Console-tab tee.
    """
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


# The dashboard is always on 127.0.0.1. Plain urlopen() honours system proxy
# settings (HTTP_PROXY, Windows' Internet Options), which would route this
# local probe through a proxy that cannot reach it, so the check never
# succeeds behind one. An opener with no proxy handlers bypasses that.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _wait_until_dashboard_ready(url, timeout=10.0, interval=0.2):
    """Give the analytics server, started on a background thread, a moment
    to bind its port before the shell tries to load it - and confirm it's
    actually *this run's* server answering, not merely something reachable
    on that port.

    A bare TCP connect can't tell those apart: if a previous copy of this
    app is still running in the background and still holding the port, it
    would accept the connection just fine, but it has its own per-launch
    auth-bypass token (see main()) and would reject `url`'s token, leaving
    the shell to embed a confusing "Authentication required" page instead
    of the dashboard. Requiring an actual HTTP 200 for `url` (which already
    carries this run's token, if any) treats that case the same as nothing
    being there at all - both are "not ready" - rather than as a success.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with _LOCAL_OPENER.open(url, timeout=interval) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(interval)
    return False


def _enable_dashboard_from_shell(config_path):
    """Shared by the one-time consent prompt and the Dashboard tab's
    on-demand "Enable dashboard" button: writes enable_analytics=True (and
    a generated password) through config_editor.py's normal AST-based edit
    path - an explicit, in-the-moment user action, unlike the silent
    first-run bootstrap in ensure_windows_analytics_defaults().

    Does not attempt to start AnalyticsServer in this already-running
    process. Settings.analytics_path is only ever set by
    TwitchChannelPointsMiner.__init__ when analytics was enabled *at
    construction time*; since it was off this run, that path never ran, and
    safely reproducing it here would mean re-implementing that setup
    (including its data-migration step) against a different, unrelated
    module's private internals. A restart re-runs that setup correctly
    instead, so this only ever saves the config and reports that a restart
    is needed.

    Returns (success, message) - message is meant to be shown to the user
    as-is, in either a dialog or the Dashboard tab's status line.
    """
    try:
        from TwitchChannelPointsMiner.config_editor import (
            ConfigEditError,
            enable_analytics_dashboard,
        )

        enable_analytics_dashboard(config_path, secrets.token_urlsafe(18))
    except (ConfigEditError, OSError) as error:
        return False, f"Could not enable the dashboard: {error}"
    return True, "Saved. Restart the app for the dashboard to start."


def _maybe_prompt_to_enable_analytics(
    window, dashboard_info, config_path, prompt_marker
):
    """One-time consent prompt for an *existing* user whose config already
    has analytics off - shown at most once ever, regardless of the answer,
    tracked by `prompt_marker` (separate from the onboarding marker, which
    tracks something else: whether the Dashboard tab has been opened to the
    Config view before).

    Separate code path from ensure_windows_analytics_defaults(), which only
    ever runs on a config this run just created; this runs for a config
    that already existed with analytics off, for any reason.
    """
    if dashboard_info is not None:
        return
    if prompt_marker.is_file():
        return

    try:
        wants_enable = window.create_confirmation_dialog(
            "Enable the dashboard?",
            "The dashboard is currently turned off. Enable it now?",
        )
    except Exception:
        # The dialog itself couldn't be shown - don't mark this as
        # "answered" so a future launch (e.g. once WebView2 is fixed) can
        # still offer it.
        return

    try:
        prompt_marker.touch()
    except OSError:
        pass

    if not wants_enable:
        return

    _success, message = _enable_dashboard_from_shell(config_path)
    try:
        window.create_confirmation_dialog("Dashboard", message)
    except Exception:
        pass


def _dashboard_url_with_bypass(url):
    """Append the per-launch shell auth-bypass token (see main()) to a
    dashboard URL, if one was set, so the embedded iframe (or the "Open in
    browser" button) never has to prompt for Basic Auth credentials the
    user just generated for themselves and has no reason to type back in.

    A plain iframe/browser navigation can't attach a custom header, so this
    rides along as a query parameter instead; AnalyticsServer's
    require_authentication() persists it into a cookie on first use so the
    dashboard's own later same-origin fetch() calls keep authenticating
    without needing to repeat it.
    """
    token = os.environ.get(SHELL_BYPASS_TOKEN_ENV_VAR)
    if not token:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}shell_token={token}"


def _acquire_single_instance_lock():
    """True if this process is the only running instance.

    Backed by a named kernel mutex rather than anything file- or
    port-based: creation is atomic, so two processes racing to start at
    the same moment (e.g. a login-triggered autostart alongside a manual
    double-click) can never both see "I'm first". The handle is
    deliberately never closed - Windows releases it automatically when
    this process exits, and holding it open for the process lifetime is
    exactly the intended "is the app still running" signal.

    Always True on non-Windows (nothing here is meaningful without the
    Windows-only ctypes.windll), so this stays a no-op for tests/dev.
    """
    global _single_instance_handle
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            # Access denied means a mutex of that name exists that this
            # process may not open fully, so an instance is running. Any
            # other failure fails open rather than blocking the launch.
            return kernel32.GetLastError() != _ERROR_ACCESS_DENIED
        if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            return False
        _single_instance_handle = handle
        return True
    except Exception:
        return True


def _release_single_instance_lock():
    """Close the single-instance mutex so another process can take it."""
    global _single_instance_handle
    handle, _single_instance_handle = _single_instance_handle, None
    if handle and os.name == "nt":
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


def _notify_running_instance():
    """Best-effort: ask the already-running instance (see
    _start_single_instance_listener) to bring itself to the foreground.

    Silently gives up on any failure - e.g. an older already-running build
    without this listener, or the port being unexpectedly taken by
    something else. Either way the user still gets the "already running"
    message box from main(); this is a nicety on top of that, not the
    only feedback they get.
    """
    try:
        with socket.create_connection(
            (_SINGLE_INSTANCE_HOST, _SINGLE_INSTANCE_PORT), timeout=1
        ):
            pass
    except OSError:
        pass


def _start_single_instance_listener(show):
    """Runs for the app's lifetime: any connection on this loopback port
    means a second launch wants us to come to the foreground (see
    _notify_running_instance, the client side of this).

    A closed connection carries no payload - just connecting is the whole
    signal - so there's nothing to parse and nothing a local unprivileged
    process could send to make this do anything other than show the
    window it could already show itself via the tray.

    Takes a `show` callable rather than the window directly so the caller
    can route it through the same quit-in-progress guard as the tray's own
    Show item - see `_make_close_confirmation_handler`'s `quitting` event.
    """

    def _serve():
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((_SINGLE_INSTANCE_HOST, _SINGLE_INSTANCE_PORT))
            server.listen(1)
        except OSError:
            return  # Port unavailable - not fatal, just no remote-show support.
        while True:
            try:
                conn, _address = server.accept()
            except OSError:
                return
            conn.close()
            show()

    threading.Thread(
        target=_serve, name="Single instance listener", daemon=True
    ).start()


def _build_tray_icon(on_show, on_quit):
    """Build a (not-yet-running) system tray icon with Show/Quit actions.

    Imports pystray/Pillow lazily, the same way launch_shell() imports
    webview lazily - so this module stays importable on platforms/test
    environments where the Windows-only tray dependency isn't installed.
    Any failure here (missing dependency, unreadable icon, no tray support
    on this desktop) is the caller's signal to fall back to the older
    confirm-on-close behavior rather than leave the app unclosable.

    "Show" is marked as pystray's `default` menu item - on Windows, that's
    the item invoked when the tray icon itself is double-clicked, not just
    a marker for how it's drawn in the menu. Without it, double-clicking
    the icon does nothing at all: Windows only ever activates the menu's
    default item, and pystray otherwise leaves no item marked as one.
    """
    import pystray
    from PIL import Image

    image = Image.open(bundled_file(os.path.join("assets", _TRAY_ICON_FILE)))
    menu = pystray.Menu(
        pystray.MenuItem("Show", lambda icon, item: on_show(), default=True),
        pystray.MenuItem("Quit", lambda icon, item: on_quit()),
    )
    return pystray.Icon(
        "TwitchChannelPointsMiner", image, "Twitch Channel Points Miner", menu
    )


def _make_hide_to_tray_handler(window, tray_icon):
    """Build a `window.events.closing` handler that hides the window to the
    tray instead of closing it - mining is unaffected, so unlike the older
    confirm-on-close handler this never needs to ask anything. Quitting for
    real now only ever happens via the tray icon's own Quit action (see
    launch_shell), which is where that confirmation moved to.

    Fires a tray balloon/toast notification each time, since hiding the
    window gives no other feedback that the app is still running rather
    than having just closed - easy to mistake for a quit, especially the
    first time. Best-effort: pystray's HAS_NOTIFICATION is False on some
    platforms/backends, and a notification failing is never worth blocking
    the hide itself over.
    """

    def on_closing():
        logger.info(
            "Close button pressed: hiding the window to the tray instead of quitting."
        )
        window.hide()
        try:
            tray_icon.notify(
                "Still running and mining in the background. "
                "Use the tray icon's Quit to stop it.",
                "Twitch Channel Points Miner",
            )
        except Exception:
            pass
        return False

    return on_closing


def _shell_prefs_path(config_path):
    """Where the Settings tab's own small preferences (currently just the
    close behavior) live - beside config.py, so it moves with the rest of a
    user's data the same way config/cookies/analytics already do, without
    needing its own directory-resolution logic."""
    return config_path.parent / _SHELL_PREFS_FILENAME


def _read_shell_prefs(config_path):
    try:
        prefs = json.loads(_shell_prefs_path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # Valid JSON that isn't an object (e.g. "[]") would break every caller's
    # .get(); treat it the same as an unreadable file.
    return prefs if isinstance(prefs, dict) else {}


def _read_close_behavior(config_path):
    """What the window's own close button (X) should do - "tray" (hide,
    mining keeps running - the default) or "quit" (stop mining, same
    confirm-then-close flow as the tray icon's own Quit item). Any missing
    or unrecognized value falls back to "tray", the long-standing default
    behavior."""
    value = _read_shell_prefs(config_path).get("close_behavior")
    return (
        value
        if value in (_CLOSE_BEHAVIOR_TRAY, _CLOSE_BEHAVIOR_QUIT)
        else _CLOSE_BEHAVIOR_TRAY
    )


def _write_close_behavior(config_path, value):
    """Persist the close-behavior choice. Returns (success, message), the
    same contract as _enable_dashboard_from_shell/set_autostart_enabled."""
    if value not in (_CLOSE_BEHAVIOR_TRAY, _CLOSE_BEHAVIOR_QUIT):
        return False, "Invalid value."
    prefs = _read_shell_prefs(config_path)
    prefs["close_behavior"] = value
    try:
        _shell_prefs_path(config_path).write_text(json.dumps(prefs), encoding="utf-8")
    except OSError as error:
        return False, f"Could not save this setting: {error}"
    return True, "Saved."


def _autostart_command():
    """Command line to register for "start on Windows login" - only
    meaningful for a frozen (PyInstaller) build; a source checkout has no
    stable double-clickable entry point to relaunch, so callers gate the
    whole feature on this.
    """
    return f'"{sys.executable}" {_START_MINIMIZED_FLAG}'


def _startup_shortcut_path():
    """The Startup-folder shortcut the installer's "start on login" task
    creates (see windows_installer.iss). Returns None when APPDATA is
    unset."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return (
        Path(appdata)
        / "Microsoft/Windows/Start Menu/Programs/Startup"
        / "Twitch Channel Points Miner.lnk"
    )


def is_autostart_enabled():
    """Whether this exe is currently registered to launch at Windows login,
    via the per-user Run key (no admin rights required, unlike the
    all-users equivalent) or the installer's Startup-folder shortcut."""
    if not getattr(sys, "frozen", False):
        return False
    shortcut = _startup_shortcut_path()
    if shortcut is not None and shortcut.exists():
        return True
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY_PATH) as key:
            value, _type = winreg.QueryValueEx(key, _AUTOSTART_VALUE_NAME)
    except OSError:
        return False
    return value == _autostart_command()


def set_autostart_enabled(enabled):
    """Add or remove the per-user Run key entry that launches this exe
    (minimized, straight to the tray - see _START_MINIMIZED_FLAG) at
    Windows login.

    Returns (success, message) - meant to be shown to the user as-is, the
    same contract as _enable_dashboard_from_shell.
    """
    if not getattr(sys, "frozen", False):
        return False, "Starting on login is only available in the installed app."
    try:
        import winreg
    except ImportError:
        return False, "Windows registry access is unavailable."

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key, _AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, _autostart_command()
                )
            else:
                try:
                    winreg.DeleteValue(key, _AUTOSTART_VALUE_NAME)
                except FileNotFoundError:
                    pass
    except OSError as error:
        return False, f"Could not update Windows startup settings: {error}"
    # Only one mechanism may launch the app at login, so the installer's
    # Startup-folder shortcut goes whichever way this toggle is flipped.
    shortcut = _startup_shortcut_path()
    if shortcut is not None:
        try:
            shortcut.unlink(missing_ok=True)
        except OSError as error:
            return False, f"Could not update Windows startup settings: {error}"
    return True, "Saved."


class WindowApi:
    """Bridge exposed to the shell's JavaScript as `window.pywebview.api`."""

    def __init__(
        self,
        console_buffer,
        dashboard_info,
        initial_tab,
        config_path,
        needs_username,
        start_mining,
        logs_dir,
    ):
        self._console_buffer = console_buffer
        self._dashboard_info = dashboard_info
        self._initial_tab = initial_tab
        self._config_path = config_path
        self._needs_username = needs_username
        self._start_mining = start_mining
        self._logs_dir = logs_dir
        # Set by launch_shell once it knows whether the tray icon actually
        # came up (pystray/Pillow missing, bad icon, no desktop tray, ...) -
        # unknown/False at construction time since the tray is only built
        # after this object already exists. The Settings tab uses this to
        # hide the "minimize to tray" choice when there'd be no tray to
        # minimize to.
        self._tray_available = False
        # submit_username can be reached twice (e.g. Enter pressed again
        # while the first call is still in flight); only the first call may
        # start mining.
        self._mining_lock = threading.Lock()
        self._mining_started = False

    def get_console_tail(self, since_seq=0):
        lines, next_seq = self._console_buffer.tail(int(since_seq or 0))
        return {"lines": lines, "next_seq": next_seq}

    def get_setup_info(self):
        """Whether the shell must collect a Twitch username - blocking the
        normal Dashboard/Console tabs, and mining itself - before anything
        else happens. See `submit_username`."""
        return {"needs_username": self._needs_username}

    def submit_username(self, username):
        """Save the username the user just entered and, only now, start
        mining - deliberately deferred until this point rather than started
        with the bundled template's placeholder, which would just fail
        Twitch login immediately on every first run.
        """
        try:
            from TwitchChannelPointsMiner.config_editor import (
                ConfigEditError,
                set_miner_username,
            )

            set_miner_username(self._config_path, username)
        except (ConfigEditError, OSError) as error:
            return {"success": False, "message": str(error)}
        self._needs_username = False
        with self._mining_lock:
            if not self._mining_started:
                self._mining_started = True
                self._start_mining()
        return {"success": True, "message": None}

    def get_dashboard_info(self):
        if not self._dashboard_info:
            return {"url": None, "enabled": False}
        url = _dashboard_url_with_bypass(self._dashboard_info["url"])
        if not _wait_until_dashboard_ready(url):
            return {
                "url": None,
                "enabled": True,
                "message": (
                    "The dashboard didn't respond as expected. Another "
                    "program - or a previous copy of this app still running "
                    "in the background - may already be using its port. "
                    "Check the Console tab for details."
                ),
            }
        return {
            "url": url,
            "initial_tab": self._initial_tab,
            "enabled": True,
        }

    def open_in_browser(self):
        if self._dashboard_info:
            webbrowser.open(_dashboard_url_with_bypass(self._dashboard_info["url"]))

    def open_twitch_activate(self):
        webbrowser.open("https://www.twitch.tv/activate")

    def open_external_url(self, url):
        """Open an arbitrary http(s) link from the embedded dashboard iframe
        in the user's real default browser.

        The dashboard runs inside a same-origin-with-itself but
        cross-origin-with-the-shell iframe, so a plain `target="_blank"`
        link there tries to open a new pywebview-native popup window
        instead of a normal browser tab - which has no default browser
        association and just shows blank. The dashboard's own JS posts
        these clicks up to the shell (see windows_shell.html's `message`
        listener), which calls this instead.

        Restricted to http(s) so this can't be used to launch arbitrary
        local files or other URI schemes - not a strong security boundary
        (the dashboard content is our own template, not attacker-supplied),
        just a sensible guard against a malformed or unexpected URL.
        """
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            webbrowser.open(url)

    def enable_dashboard(self):
        success, message = _enable_dashboard_from_shell(self._config_path)
        return {"success": success, "message": message}

    def open_config_folder(self):
        _open_folder(self._config_path.parent)

    def open_logs_folder(self):
        _open_folder(self._logs_dir)

    def get_autostart_info(self):
        return {
            "available": bool(getattr(sys, "frozen", False)),
            "enabled": is_autostart_enabled(),
        }

    def set_autostart(self, enabled):
        success, message = set_autostart_enabled(bool(enabled))
        return {"success": success, "message": message}

    def get_close_behavior_info(self):
        return {
            "available": self._tray_available,
            "value": _read_close_behavior(self._config_path),
        }

    def set_close_behavior(self, value):
        success, message = _write_close_behavior(self._config_path, value)
        return {"success": success, "message": message}

    def get_about_info(self):
        return {
            "version": __version__,
            "commit": _build_commit_hash(),
        }


def _open_folder(path):
    """Best-effort: open `path` in the OS file manager, creating it first if
    it doesn't exist yet (e.g. the logs folder before the miner has written
    anything). Silently does nothing if that fails - not worth surfacing an
    error dialog over.

    Gated on hasattr(os, "startfile") rather than os.name == "nt": the
    attribute genuinely only exists on a Windows CPython build, so it's
    equivalent in production, but doesn't require flipping the real
    (process-wide) os.name to test - which pathlib itself also reads to
    pick Path's concrete subclass, and does not uniformly tolerate being
    told to build the "wrong" one for the actual OS across Python versions.
    """
    try:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(path)  # noqa: S606 - Windows-only, opens Explorer
    except OSError:
        pass


def _run_miner_thread(argv, on_miner_ready):
    """Thread target wrapping runner_main() the way runner.cli() wraps it for
    the normal synchronous entry point - without this, a RuntimeError raised
    before mining starts (e.g. failure to create the default config) would
    propagate unhandled off the main thread and never reach the user: the
    shell window would just sit there with mining silently never started.
    """
    try:
        runner_main(argv, on_miner_ready=on_miner_ready)
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        _show_fatal_error_message(
            f"Twitch Channel Points Miner failed to start:\n\n{error}"
        )


class _MinerThreadHandle:
    """Shares a reference to the miner thread between the close-confirmation
    handler below and the first-run setup flow, which defers actually
    creating and starting that thread until a real username is entered (see
    `_needs_username` and `WindowApi.submit_username`) - so at the time the
    handler is wired up, there may not be a thread yet.
    """

    def __init__(self):
        self.thread = None
        self.miner = None

    def is_alive(self):
        return self.thread is not None and self.thread.is_alive()

    def set_miner(self, miner):
        self.miner = miner


def _make_close_confirmation_handler(
    window, miner_thread, quitting=None, tray_icon=None
):
    """Build a `window.events.closing` handler that blocks the close unless
    the user confirms, whenever the miner is still running.

    `tray_icon`, when given, fires a "shutting down" balloon/toast the
    moment the user confirms - `_stop_miner_gracefully` below runs
    synchronously on this same (GUI) thread and can legitimately take up to
    a few minutes (IRC leave, websocket teardown, several 30-60s watcher
    joins), during which the window itself is unresponsive and looks hung.
    The notification is rendered by the shell (explorer.exe), not this
    process, so it stays visible through that blocked stretch. Best-effort,
    matching this file's other tray_icon.notify use in
    _make_hide_to_tray_handler: never worth failing the quit over.

    Before this shell existed, stopping the miner required an explicit
    Ctrl+C; a bare click on the window's close button must not silently end
    an unattended, hours-long mining session. Returning False from a
    pywebview `closing` handler cancels the close.

    `miner_thread` only needs an `is_alive()` method - a plain Thread, or a
    _MinerThreadHandle for a thread that may not exist yet, both work.

    Guarded by a non-blocking lock against reentrant calls: pywebview's
    WinForms confirmation dialog is a bare `MessageBox.Show()` with no
    owner window, so it does not disable the underlying form - clicking
    the window's close button again (or tray Quit again) while it's up
    re-fires WinForms' FormClosing before this handler has returned.
    Without the guard, that reentrant call would show a second dialog and
    - if confirmed - actually close/dispose the form from inside the
    nested call; the outer call then resumes and tries to close the
    (already-disposed) form again, which crashes pywebview trying to
    remove the same window uid from its instance table twice.

    `quitting` (a threading.Event) is set for the duration of the dialog
    and left set once the user confirms - see launch_shell's `_guarded_show`.
    An unowned MessageBox doesn't just let a second *close* reach the form
    (the reentrancy above); it also leaves the tray's "Show" item, and the
    single-instance listener's remote-show signal, free to fire from their
    own threads while this dialog is up. `window.show()` marshals onto the
    GUI thread via Control.Invoke, which a nested modal message loop like
    MessageBox.Show still pumps - so it can run interleaved with this
    handler tearing the window down, producing the same
    already-removed-from-pywebview's-instance-table crash as the reentrant
    case above, just reached via Show instead of a second Close. Cleared
    again if the user cancels, so Show keeps working normally afterward.
    Defaults to a fresh, private Event when the caller has no tray (and
    thus no Show button to race against).
    """
    in_progress = threading.Lock()
    if quitting is None:
        quitting = threading.Event()

    def on_closing():
        if not miner_thread.is_alive():
            return True
        if not in_progress.acquire(blocking=False):
            return False
        quitting.set()
        confirmed = False
        try:
            try:
                confirmed = window.create_confirmation_dialog(
                    "Stop mining?",
                    "Closing this window will stop mining. Are you sure?",
                )
            except Exception:
                # If the dialog itself can't be shown, err on the side of NOT
                # silently stopping an unattended miner.
                return False
            if confirmed:
                if tray_icon is not None:
                    try:
                        tray_icon.notify(
                            "Shutting down - leaving chat, closing connections, and "
                            "saving data. This can take a moment.",
                            "Twitch Channel Points Miner",
                        )
                    except Exception:
                        pass
                _stop_miner_gracefully(miner_thread)
            return confirmed
        finally:
            if not confirmed:
                quitting.clear()
            in_progress.release()

    return on_closing


def _make_close_dispatcher(get_close_behavior, hide_handler, quit_handler):
    """Build the normal (non-tray-Quit) `window.events.closing` handler,
    routing to hide-to-tray or confirm-and-quit based on the user's saved
    close-behavior preference (see _read_close_behavior/_write_close_behavior,
    exposed through the Settings tab). Re-read on every close rather than
    captured once, so a preference change takes effect immediately without
    restarting the app."""

    def on_closing():
        if get_close_behavior() == _CLOSE_BEHAVIOR_QUIT:
            return quit_handler()
        return hide_handler()

    return on_closing


def _stop_miner_gracefully(miner_thread_handle):
    """Runs the same clean shutdown a SIGINT/SIGTERM would have triggered
    (IRC chat leave, websocket pool teardown, watcher-thread joins, final
    report) before the window is allowed to close.

    Needed because the miner runs on a background daemon thread here, and
    TwitchChannelPointsMiner._register_signal_handlers() is a deliberate
    no-op off the main thread - so nothing else ever calls end() for this
    shell. Runs on the GUI thread while the miner thread is mid-loop; end()
    already guards its state (streamer mutex checks, running flags) for
    exactly this kind of concurrent call. Its trailing sys.exit(0) is caught
    below since it would only ever unwind this thread's dialog handler, not
    the process - the process exits normally once the window finishes
    closing.
    """
    miner = getattr(miner_thread_handle, "miner", None)
    if miner is None:
        # The miner thread reports itself alive as soon as it starts, but
        # doesn't populate `.miner` until on_miner_ready() fires - after
        # config load and campaign construction. A quit confirmed in that
        # window would otherwise abandon the thread with no graceful
        # shutdown at all; wait briefly for it instead of giving up at once.
        deadline = time.monotonic() + 15
        while miner is None and time.monotonic() < deadline:
            if not miner_thread_handle.is_alive():
                return
            time.sleep(0.1)
            miner = getattr(miner_thread_handle, "miner", None)
        if miner is None:
            return
    try:
        miner.end(None, None)
    except SystemExit:
        pass
    except Exception as error:
        print(f"Error while stopping the miner gracefully: {error}", file=sys.stderr)


def launch_shell(
    dashboard_info,
    console_buffer,
    initial_tab,
    miner_thread,
    config_path,
    prompt_marker,
    needs_username,
    start_mining,
    logs_dir,
    start_minimized=False,
):
    """Open the two-tab desktop shell (Dashboard + Console) - or, on a first
    run with no configured Twitch username yet, a setup screen that
    collects one via `start_mining` before either tab (and mining itself)
    starts. See `_needs_username` and `WindowApi.submit_username`.

    Imports pywebview lazily so this module stays importable - and
    unit-testable - on platforms/environments where the Windows-only
    dependency isn't installed.
    """
    import webview

    # pywebview's own logger already has a StreamHandler attached (added at
    # import time in webview/__init__.py) writing to whatever sys.stderr was
    # at that point - which, since install_console_capture() already ran by
    # now, is the Console tab's tee. It defaults to INFO, which swallows the
    # one debug line that says which native renderer got picked (CEF /
    # WebView2 "edgechromium" / legacy "mshtml" - see webview/platforms/
    # winforms.py) and any other library-internal debug detail. That
    # renderer choice is exactly the kind of thing that could make the
    # minimize/restore diagnostics below behave differently across machines,
    # so surface it rather than leaving it hidden.
    logging.getLogger("pywebview").setLevel(logging.DEBUG)
    logger.info("Shell using pywebview %s.", getattr(webview, "__version__", "unknown"))

    api = WindowApi(
        console_buffer,
        dashboard_info,
        initial_tab,
        config_path,
        needs_username,
        start_mining,
        logs_dir,
    )
    shell_html = bundled_file(os.path.join("assets", "windows_shell.html")).read_text(
        encoding="utf-8"
    )
    # windows_shell.html is loaded below via html=..., a raw string with no
    # base URL a <script src> could resolve against - inline the tail-buffer
    # helper shared with the dashboard's Logs tab (assets/script.js) directly
    # into its one inline <script> block instead of duplicating its logic.
    tail_buffer_js = bundled_file(os.path.join("assets", "tail_buffer.js")).read_text(
        encoding="utf-8"
    )
    shell_html = shell_html.replace("<script>", f"<script>\n{tail_buffer_js}", 1)
    window = webview.create_window(
        f"Twitch Channel Points Miner - {__version__}",
        html=shell_html,
        js_api=api,
        width=1200,
        height=800,
        min_size=(800, 600),
        # pywebview disables in-page text selection by default; the Console
        # tab exists specifically so logs can be read (and copied) here
        # instead of a separate window, so that default would defeat its
        # purpose.
        text_select=True,
        # Start-on-login launches straight to the tray rather than popping a
        # window on every boot - but never while first-run setup still needs
        # to collect a username (see main()'s start_minimized gating).
        hidden=start_minimized,
    )

    # Diagnostic-only: some users report the window occasionally not
    # reappearing as a taskbar button after minimizing it, but nothing in
    # this file (or pywebview) actually touches taskbar visibility on
    # minimize/restore - that's left entirely to the OS/WinForms default.
    # These just record that the events fired at all, so a report can be
    # correlated against the log: e.g. a "minimized" with no matching
    # "restored" narrows it to an OS/WebView2-level rendering quirk rather
    # than this app swallowing the restore.
    #
    # Reports have also come in of the minimize click producing *no* log
    # line at all, which the two one-line lambdas this used to be couldn't
    # help diagnose any further - there was nothing to distinguish "the
    # handler never ran" from "it ran and logger.info() itself failed". Named
    # handlers wrapped in try/except close that gap: pywebview's Event.set()
    # already catches handler exceptions (logging them to its own
    # 'pywebview' logger, now surfaced above), but that would only prove a
    # handler ran and blew up, not that Resize/on_resize fired at all.
    def _log_window_event(name):
        def _handler(*args, **kwargs):
            try:
                logger.info(
                    "Shell window event fired: %s (args=%r, kwargs=%r)",
                    name,
                    args,
                    kwargs,
                )
            except Exception:
                logger.exception("Failed to log shell window event: %s", name)

        return _handler

    window.events.minimized += _log_window_event("minimized")
    window.events.restored += _log_window_event("restored")
    logger.info("Registered minimize/restore diagnostics for the shell window.")

    # Set for as long as a close/quit confirmation dialog is up (and left set
    # once the user confirms) - see _make_close_confirmation_handler's
    # docstring for the race this closes between that dialog and a
    # concurrent Show request from the tray icon's own thread or the
    # single-instance listener's socket thread.
    quitting = threading.Event()

    def _guarded_show():
        if quitting.is_set():
            return
        try:
            window.show()
        except Exception:
            # Best-effort, matching _message_box/tray_icon.notify elsewhere
            # in this file: the is_set() check above and the underlying
            # pywebview call it guards aren't atomic with on_closing setting
            # `quitting` - a Show can still be in flight (already past the
            # check, already marshaled onto the GUI thread via
            # Control.Invoke) at the exact moment the window gets torn down.
            # Swallowing here keeps that vanishingly rare, already-late Show
            # from raising back into a caller that must not die over it: the
            # tray icon's own callback thread, or _start_single_instance_listener's
            # daemon thread, which would otherwise silently and permanently
            # stop responding to second-launch pings for the rest of this run.
            pass

    _start_single_instance_listener(_guarded_show)

    tray_icon = None
    normal_handler = None
    quit_handler = None
    confirm_from_tray_lock = threading.Lock()

    def _confirm_and_quit_from_tray():
        """A `window.events.closing` handler, temporarily swapped in by
        `_quit_from_tray` below in place of the normal handler.

        Critically, this makes it run on pywebview's actual GUI thread:
        `events.closing` fires from inside WinForms' own FormClosing event,
        itself reached only through `destroy()`, which pywebview properly
        marshals onto that thread (`Control.Invoke`). pystray's Quit menu
        item, by contrast, calls back on pystray's *own* icon thread - and
        `create_confirmation_dialog` on Windows is a bare, unmarshaled
        `MessageBox.Show()` with no thread-affinity handling of its own.
        Calling it directly from there (as this used to) could leave the
        whole app stuck after the dialog was answered, with no way to
        actually quit short of killing the process. Routing through this
        event instead keeps the entire confirm-then-stop sequence on the
        thread WinForms actually expects it on.

        Stays registered on `window.events.closing` for the whole call,
        including while `quit_handler()` is blocked showing its
        confirmation dialog - removing it first (as this used to) leaves
        no handler registered during that window, so a reentrant close
        (a real, unowned WinForms MessageBox doesn't stop clicks from
        reaching the underlying form - e.g. its own [X] clicked again
        while this dialog is up) would sail through uncancelled instead of
        being cancelled here. Guarded by its own lock, acquired before
        calling `quit_handler()` and released after, so that reentrant
        call - reached via a nested `events.closing.set()` while this one
        is still on the stack - is cancelled outright without touching
        event registration at all; only the outer call ever adds/removes
        handlers.
        """
        if not confirm_from_tray_lock.acquire(blocking=False):
            return False
        try:
            confirmed = quit_handler()
        finally:
            confirm_from_tray_lock.release()
        if not confirmed:
            # Cancelled - restore the normal close behavior for the next
            # close/hide, whether that's this same tray Quit tried again or
            # the window's own close button.
            window.events.closing -= _confirm_and_quit_from_tray
            window.events.closing += normal_handler
            return False
        window.events.closing -= _confirm_and_quit_from_tray
        if tray_icon is not None:
            tray_icon.stop()
        return True

    def _quit_from_tray():
        window.events.closing -= normal_handler
        window.events.closing += _confirm_and_quit_from_tray
        window.destroy()

    try:
        tray_icon = _build_tray_icon(_guarded_show, _quit_from_tray)
    except Exception:
        # No tray support available (dependency missing, no desktop tray,
        # bad icon, ...) - fall back to the older confirm-on-close behavior
        # rather than leave the app with no way to quit at all.
        tray_icon = None
        logger.exception(
            "System tray icon unavailable; falling back to confirm-on-close."
        )

    quit_handler = _make_close_confirmation_handler(
        window, miner_thread, quitting, tray_icon
    )

    api._tray_available = tray_icon is not None

    if tray_icon is not None:
        threading.Thread(target=tray_icon.run, name="Tray icon", daemon=True).start()
        hide_handler = _make_hide_to_tray_handler(window, tray_icon)
        normal_handler = _make_close_dispatcher(
            lambda: _read_close_behavior(config_path), hide_handler, quit_handler
        )
    else:
        normal_handler = quit_handler
    window.events.closing += normal_handler

    def _on_started():
        # Dialogs (unlike event handlers) require the GUI loop that
        # webview.start() begins - pywebview's own examples run them via
        # this callback, not before start() is called.
        #
        # webview.guilib is only populated once webview.start() actually
        # picks a backend, hence logging the renderer here rather than
        # alongside the minimize/restore diagnostics above, which are
        # registered before start() runs.
        logger.info(
            "Shell GUI loop started (renderer=%s).",
            getattr(getattr(webview, "guilib", None), "renderer", "unknown"),
        )
        if start_minimized and tray_icon is None:
            # Created hidden on the assumption the tray would bring it back;
            # with no tray there is no other way to reach the window, so show
            # it. window.show() is called here, not before start(), because
            # pywebview only accepts window operations once its GUI loop runs.
            window.show()
        if not needs_username:
            # Asking about the dashboard before the user has even entered a
            # username would be premature - it can still appear on a later,
            # normal launch once one is set.
            _maybe_prompt_to_enable_analytics(
                window, dashboard_info, config_path, prompt_marker
            )

    # Without this, pywebview falls back to extracting whatever icon
    # Windows associates with sys.executable at runtime - fragile for a
    # frozen onefile build and simply wrong (python.exe's icon) when run
    # unfrozen from source. Passing it explicitly is the documented,
    # reliable way to get the app's own icon on the actual running window
    # (taskbar/Alt-Tab), separate from the exe *file's* icon in Explorer,
    # which PyInstaller's --icon flag in build_windows.bat already covers.
    webview.start(
        _on_started, icon=str(bundled_file(os.path.join("assets", _TRAY_ICON_FILE)))
    )


# --- Background mode -------------------------------------------------------
#
# `--background` runs mining with no window, from a Task Scheduler entry (or a
# Run key fallback) at sign-in. The process started by the scheduler is a small
# supervisor; it runs the real miner as a `--background-worker` child, so a
# crash in the miner is recovered by restarting the child with a backoff
# instead of ending the whole background session.


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _background_log_path():
    # Callers have already chdir'd into the application directory.
    return Path.cwd() / "logs" / "background.log"


def _background_stop_path():
    return Path.cwd() / _BACKGROUND_STOP_FILE


def _remove_background_stop_file():
    try:
        _background_stop_path().unlink()
    except OSError:
        pass  # Usually just "not there", which is the goal anyway.


def _rotate_log_if_large(path, max_bytes):
    """Move an oversized log aside to <name>.prev so a long-running unattended
    install does not grow one log file without limit."""
    try:
        if path.stat().st_size > max_bytes:
            path.replace(path.with_name(path.name + ".prev"))
    except OSError:
        pass


def _background_log(message):
    """Append one timestamped line to logs/background.log.

    The windowed exe has no console, so this file is the only record of an
    unattended run. Logging must never be what crashes that run, hence the
    broad OSError guard.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
    try:
        path = _background_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
    except OSError:
        pass


def _no_window_flags():
    """Popen/run keyword arguments that stop a console window appearing for
    the child. Empty off Windows, where creationflags is not accepted."""
    return {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}


def _acquire_background_mutex():
    """Create the background-instance mutex and keep its handle open for the
    process lifetime, the same pattern as _acquire_single_instance_lock.

    Returns False only if it already existed, meaning another background
    supervisor is running. The main single-instance lock is taken first, so
    this is a backstop rather than the primary guard.
    """
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, _BACKGROUND_MUTEX_NAME)
        if not handle:
            return True
        return kernel32.GetLastError() != _ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _background_instance_running():
    """True while a background supervisor holds _BACKGROUND_MUTEX_NAME.

    Opening (rather than creating) the mutex means this never takes ownership
    itself; the handle is closed straight away.
    """
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, _BACKGROUND_MUTEX_NAME)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def _sign_in_hint():
    """Where the user goes to set up a username and the Twitch sign-in."""
    if _is_headless_build():
        return 'Run "Set up background mining" from the Start menu once to sign in.'
    return "Open the desktop app once to sign in."


def _background_setup_problem(config_path, application_dir):
    """Why background mining can't start unattended yet, or None when it can.

    Background mode cannot show Twitch's activation code or ask for a
    username, so both must already be done, in the desktop app or the
    background build's --setup console. Each reason names the fix.
    """
    if _needs_username(config_path):
        return f"No Twitch username is configured. {_sign_in_hint()}"
    try:
        signed_in = _twitch_session_saved(config_path, application_dir)
    except (OSError, SyntaxError):
        return f"Could not read {config_path}; background mining not started."
    if not signed_in:
        return f"Not signed in to Twitch yet. {_sign_in_hint()}"
    return None


def _background_ready(config_path, application_dir):
    """True if background mining can start unattended; otherwise logs why."""
    problem = _background_setup_problem(config_path, application_dir)
    if problem:
        _background_log(problem)
        return False
    return True


def _twitch_session_saved(config_path, application_dir):
    """True once a Twitch login for the configured username is saved.

    Raises OSError or SyntaxError when the configuration can't be read.
    """
    source = config_path.read_text(encoding="utf-8")
    username = read_config_literal(source, "MINER_CONFIG", "username")
    android_login = read_config_literal(
        source, "MINER_CONFIG", "android_login", default=False
    )
    # Twitch.py stores the Android login under its own cookie file, so check
    # the file for the configured login style first. Either one is enough.
    cookie_names = [username, f"{username}-android"]
    if android_login:
        cookie_names.reverse()
    cookies_dir = application_dir / "cookies"
    return any((cookies_dir / f"{name}.pkl").is_file() for name in cookie_names)


def _kill_worker_with_parent(proc):
    """Put the worker in a Job Object set to kill its processes when the job's
    last handle closes. The parent holds that handle for its whole life, so
    ending the parent (e.g. Task Scheduler's "End") also ends the worker,
    instead of leaving an orphaned miner running.

    Best-effort: any failure is logged and the worker simply is not tied to
    the parent. A no-op off Windows.
    """
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            _background_log(
                "Could not create a job object; the worker will not follow this process."
            )
            return
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _background_log(
                "Could not configure the job object; the worker will not follow this process."
            )
            return
        if not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
            _background_log(
                "Could not put the worker in the job object; it will not follow this process."
            )
            return
        _background_job_handles.append(job)
    except Exception as error:
        _background_log(f"Could not tie the worker to this process: {error}")


def _start_background_worker():
    """Launch one `--background-worker` child in the application directory."""
    env = {**os.environ, "TCPM_RESTART_EXIT_CODE": str(_BACKGROUND_RESTART_EXIT_CODE)}
    proc = subprocess.Popen(
        [sys.executable, _BACKGROUND_WORKER_FLAG],
        cwd=str(Path.cwd()),
        env=env,
        **_no_window_flags(),
    )
    _kill_worker_with_parent(proc)
    return proc


def _wait_background_backoff(seconds):
    """Wait out a restart delay, checking for a stop request every second.

    Returns True if a stop was requested. The stop file is consumed here so a
    later launch is not stopped by a stale request.
    """
    deadline = _background_clock() + seconds
    while _background_clock() < deadline:
        if _background_stop_path().exists():
            _remove_background_stop_file()
            return True
        _background_sleep(_BACKGROUND_POLL_SECONDS)
    return False


def _supervise_background_worker():
    """Run the worker, restarting it as needed. Returns the supervisor's exit
    code.

    A worker exit of 0 ends the session. Exit code 3 is the miner asking for
    a restart, so it restarts at once with no backoff. Any other exit is a
    crash: restart after a delay that doubles with each consecutive crash, up
    to _BACKGROUND_BACKOFF_MAX. A worker that ran for a long time resets that
    streak. While the worker runs, the worker itself watches for the stop
    file, so this loop only has to wait for it to exit.
    """
    backoff = _BACKGROUND_BACKOFF_START
    while True:
        started = _background_clock()
        proc = _start_background_worker()
        code = proc.wait()
        ran_for = _background_clock() - started
        if code == 0:
            _background_log("Background mining stopped.")
            return 0
        if code == _BACKGROUND_RESTART_EXIT_CODE:
            _background_log("Restarting (requested by the miner).")
            continue
        if ran_for >= _BACKGROUND_HEALTHY_RUN_SECONDS:
            backoff = _BACKGROUND_BACKOFF_START
        _background_log(
            f"Background worker exited with code {code}; restarting in {backoff} seconds."
        )
        if _wait_background_backoff(backoff):
            _background_log(
                "Stop requested during the restart delay. Background mining stopped."
            )
            return 0
        backoff = min(backoff * 2, _BACKGROUND_BACKOFF_MAX)


def _background_main():
    """`--background`: the supervising parent, started at sign-in.

    Holds the main single-instance lock and the background mutex for its whole
    life. Exits quietly (no message box - nobody is there to dismiss it) when
    the setup is incomplete or another instance is already running.
    """
    exe_dir, application_dir = _prepare_application_directories()
    if not _acquire_single_instance_lock():
        _background_log(
            "Twitch Channel Points Miner is already running; background mining not started."
        )
        return 0
    if not _acquire_background_mutex():
        _background_log("Background mining is already running.")
        return 0
    _rotate_log_if_large(_background_log_path(), _BACKGROUND_LOG_MAX_BYTES)
    # A stop file left over from an earlier session must not stop this one.
    # Deleted only now that the lock is ours, so a refused launch cannot
    # discard a stop request meant for the instance that is running.
    _remove_background_stop_file()
    _background_log("Background launcher starting.")

    if _is_standard_build():
        # Migration notices are only for the desktop window; there is nobody
        # here to read one.
        _migrate_legacy_windows_data(exe_dir, application_dir)
    config_dir, _created = prepare_config(application_dir)
    config_path = config_dir / "config.py"
    if not _background_ready(config_path, application_dir):
        return 0

    _background_log("Background mining starting.")
    return _supervise_background_worker()


def _redirect_worker_output(log_path):
    """Send stdout/stderr to the worker's log file. Kept separate so tests can
    skip it - redirecting the real streams would break pytest's capture."""
    stream = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


def _background_worker_main():
    """`--background-worker`: runs the miner once, without a window.

    Started by _background_main, which holds the single-instance lock - so
    this does not take one itself. Returns 0 when a stop was requested and
    the miner shut down cleanly, and 1 when the miner ended on its own; the
    parent treats 1 as a crash and restarts with backoff.
    """
    global _headless_worker
    exe_dir, application_dir = _prepare_application_directories()
    log_path = application_dir / "logs" / "background-worker.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log_if_large(log_path, _BACKGROUND_WORKER_LOG_MAX_BYTES)
        _redirect_worker_output(log_path)
    except OSError:
        pass  # Keep going with whatever streams exist rather than die here.
    _headless_worker = True

    config_dir, _created = prepare_config(application_dir)
    argv = [
        "--config-dir",
        str(config_dir),
        "--legacy-runner",
        str(exe_dir / "run.py"),
    ]
    handle = _MinerThreadHandle()
    thread = threading.Thread(
        target=_run_miner_thread,
        args=(argv, handle.set_miner),
        name="Miner runner",
        daemon=True,
    )
    handle.thread = thread
    thread.start()
    print("Background worker started.", flush=True)

    while True:
        if _background_stop_path().exists():
            _remove_background_stop_file()
            print("Stop requested; shutting down background mining.", flush=True)
            _stop_miner_gracefully(handle)
            thread.join(_BACKGROUND_STOP_TIMEOUT)
            return 0
        if not thread.is_alive():
            print(
                "The miner stopped on its own; the supervisor will decide whether to restart it.",
                flush=True,
            )
            return 1
        thread.join(_BACKGROUND_POLL_SECONDS)


def _end_background_worker(code):
    """Exit a frozen worker at once with `code`.

    A normal interpreter exit waits for every non-daemon thread, and a miner
    thread that outlived its stop or crash (an IRC chat still connecting, a
    stuck watcher) would keep the worker alive with nothing to end it: the
    parent waits on the process and only restarts it once it exits. The miner
    has already shut down or crashed by now, so nothing is lost. Outside a
    frozen build (tests, a source checkout) the code is just returned.
    """
    if getattr(sys, "frozen", False):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(code)
    return code


def _request_background_stop(timeout=_BACKGROUND_STOP_TIMEOUT):
    """Ask a running background supervisor to stop, then wait for it.

    Returns True once nothing is running, or False if it is still running
    after `timeout` seconds. Callers must already be in the application
    directory.
    """
    if not _background_instance_running():
        _remove_background_stop_file()
        return True
    _background_stop_path().touch()
    deadline = _background_clock() + timeout
    while _background_instance_running():
        if _background_clock() >= deadline:
            return False
        _background_sleep(_BACKGROUND_POLL_SECONDS)
    return True


def _stop_background_main():
    """`--stop-background`: stop the background miner from the Start menu or a
    script, and wait for it to finish shutting down."""
    _prepare_application_directories()
    if not _background_instance_running():
        _remove_background_stop_file()
        _background_log("Background mining is not running.")
        return 0
    if _request_background_stop():
        _background_log("Background mining stopped.")
        return 0
    _background_log(
        f"Background mining did not stop within {_BACKGROUND_STOP_TIMEOUT} seconds."
    )
    return 1


def _powershell_single_quoted(value):
    """Quote `value` as a PowerShell single-quoted literal. Inside one, the
    only escape PowerShell recognises is a doubled single quote."""
    return "'" + value.replace("'", "''") + "'"


def _background_task_script(exe_path, task_name):
    """PowerShell that registers the at-sign-in background task for the
    current user. Built by string concatenation with every value quoted, so
    an exe path or task name containing an apostrophe cannot break out of
    its literal."""
    return (
        "$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name; "
        f"$action = New-ScheduledTaskAction -Execute {_powershell_single_quoted(exe_path)}"
        f" -Argument {_powershell_single_quoted(_BACKGROUND_FLAG)}; "
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user; "
        "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited; "
        "$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero)"
        " -MultipleInstances IgnoreNew -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -StartWhenAvailable; "
        f"Register-ScheduledTask -TaskName {_powershell_single_quoted(task_name)}"
        " -Action $action -Trigger $trigger -Principal $principal -Settings $settings"
        " -Force | Out-Null"
    )


# whoami prints the SID that goes into the task's security descriptor; only
# a well-formed SID is ever embedded there.
_SID_RE = re.compile(r"S-1-\d+(-\d+)+")


def _current_user_account():
    """(account, sid) of the user running this process, from whoami.

    Read here, before elevating, because the elevated PowerShell may run as a
    different administrator account. Raises OSError when it can't be read.
    """
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=60,
            **_no_window_flags(),
        )
    except subprocess.TimeoutExpired as error:
        raise OSError(str(error)) from error
    if result.returncode != 0:
        raise OSError(
            _first_line(result.stderr) or f"whoami exit code {result.returncode}"
        )
    rows = list(csv.reader([result.stdout.strip()]))
    if not rows or len(rows[0]) < 2 or not _SID_RE.fullmatch(rows[0][1]):
        raise OSError("could not read the current account from whoami")
    return rows[0][0], rows[0][1]


def _boot_task_script(exe_path, task_name, account, sid, error_path):
    """PowerShell, run elevated, that registers background mining to start at
    boot and at sign-in, as `account`, without storing a password (S4U).

    The task's security descriptor also grants `sid` full access, so the
    same user can start, stop and delete it later without elevating. Any
    failure message is written to `error_path`, since an elevated process's
    output can't be read back.
    """
    q = _powershell_single_quoted
    return (
        "$ErrorActionPreference = 'Stop'; "
        "try { "
        f"$action = New-ScheduledTaskAction -Execute {q(exe_path)}"
        f" -Argument {q(_BACKGROUND_FLAG)}; "
        "$triggers = @((New-ScheduledTaskTrigger -AtStartup),"
        f" (New-ScheduledTaskTrigger -AtLogOn -User {q(account)})); "
        f"$principal = New-ScheduledTaskPrincipal -UserId {q(account)}"
        " -LogonType S4U -RunLevel Limited; "
        "$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero)"
        " -MultipleInstances IgnoreNew -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -StartWhenAvailable"
        " -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
        f"Register-ScheduledTask -TaskName {q(task_name)}"
        " -Action $action -Trigger $triggers -Principal $principal"
        " -Settings $settings -Force | Out-Null; "
        "$service = New-Object -ComObject Schedule.Service; $service.Connect(); "
        f"$task = $service.GetFolder('\\').GetTask({q(task_name)}); "
        # 4 is DACL_SECURITY_INFORMATION; FA is full access.
        f"$task.SetSecurityDescriptor($task.GetSecurityDescriptor(4) + {q(f'(A;;FA;;;{sid})')}, 0); "
        "exit 0 "
        "} catch { "
        f"Set-Content -LiteralPath {q(str(error_path))} -Value $_.Exception.Message -Encoding UTF8; "
        "exit 1 "
        "}"
    )


def _elevation_error_path():
    path = Path.cwd() / "logs" / "background-elevated-error.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _run_elevated_powershell(script, error_path):
    """Run `script` in an elevated PowerShell (one UAC prompt) and wait.

    Returns (ok, detail). The detail is what the script wrote to
    `error_path`, or the launcher PowerShell's own error, such as a declined
    UAC prompt.
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    launcher = (
        "$ErrorActionPreference = 'Stop'; "
        "$process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait"
        " -PassThru -WindowStyle Hidden -ArgumentList"
        " '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',"
        f"'-EncodedCommand','{encoded}'; "
        "exit $process.ExitCode"
    )
    try:
        error_path.unlink()
    except OSError:
        pass
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                launcher,
            ],
            capture_output=True,
            text=True,
            # Long enough for someone to answer the UAC prompt.
            timeout=600,
            **_no_window_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    if result.returncode == 0:
        return True, ""
    try:
        detail = _first_line(error_path.read_text(encoding="utf-8-sig"))
        error_path.unlink()
    except OSError:
        detail = ""
    return False, (
        detail or _first_line(result.stderr) or f"exit code {result.returncode}"
    )


def _register_boot_task(exe_path):
    """Register the boot-time task. Returns (ok, detail)."""
    try:
        account, sid = _current_user_account()
    except OSError as error:
        return False, str(error)
    error_path = _elevation_error_path()
    return _run_elevated_powershell(
        _boot_task_script(exe_path, _BACKGROUND_TASK_NAME, account, sid, error_path),
        error_path,
    )


def _background_task_xml():
    """The registered background task's XML, or None when there is none."""
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", _BACKGROUND_TASK_NAME, "/XML"],
            capture_output=True,
            text=True,
            timeout=60,
            **_no_window_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _background_run_value_set():
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY_PATH) as key:
            winreg.QueryValueEx(key, _BACKGROUND_RUN_VALUE_NAME)
        return True
    except (ImportError, OSError):
        return False


def _background_start_mode():
    """How background mining starts: "boot", "sign-in", "run-key" or None."""
    xml = _background_task_xml()
    if xml is not None:
        return "boot" if "<BootTrigger" in xml else "sign-in"
    if _background_run_value_set():
        return "run-key"
    return None


def _first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _delete_background_run_value():
    """Remove the background Run entry. A missing entry is not an error;
    other registry failures are raised for the caller to report."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _BACKGROUND_RUN_VALUE_NAME)
    except FileNotFoundError:
        pass


def _finish_background_registration(mechanism):
    """Make background mode the only thing that starts at sign-in.

    The desktop app's own Run entry is removed so the window does not also
    open on login. A leftover Run-key fallback is removed when a scheduled
    task was used instead.
    """
    set_autostart_enabled(False)
    if mechanism == "task":
        try:
            _delete_background_run_value()
        except (ImportError, OSError):
            pass  # Harmless duplicate at worst; registration itself succeeded.


def register_background_task(at_boot=False):
    """Register background mining to start at sign-in, or with `at_boot` at
    boot and at sign-in.

    The boot task runs whether or not anyone is signed in, as this user
    without a stored password (S4U), and registering it needs one UAC
    prompt. When that fails or is declined, registration carries on as the
    sign-in task and the message says why.

    The sign-in task is a per-user scheduled task, which can be stopped
    from Task Scheduler and has no effect on the desktop app's Run entry.
    It falls back to a per-user Run value if the task cannot be created,
    e.g. when PowerShell or Task Scheduler is unavailable.

    Returns (ok, mechanism, message); mechanism is "boot-task", "task",
    "run-key" or None.
    """
    if at_boot:
        boot_ok, boot_detail = _register_boot_task(sys.executable)
        if boot_ok:
            _finish_background_registration("task")
            return (
                True,
                "boot-task",
                "Background mining will start when Windows starts, before "
                "anyone signs in (scheduled task).",
            )
        ok, mechanism, message = register_background_task()
        return (
            ok,
            mechanism,
            f"Could not set up starting at boot ({boot_detail}). {message}",
        )
    script = _background_task_script(sys.executable, _BACKGROUND_TASK_NAME)
    task_error = None
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            **_no_window_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        task_error = str(error)
    else:
        if result.returncode == 0:
            _finish_background_registration("task")
            return (
                True,
                "task",
                "Background mining will start at sign-in (scheduled task).",
            )
        task_error = _first_line(result.stderr) or f"exit code {result.returncode}"

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(
                key,
                _BACKGROUND_RUN_VALUE_NAME,
                0,
                winreg.REG_SZ,
                f'"{sys.executable}" {_BACKGROUND_FLAG}',
            )
    except (ImportError, OSError) as error:
        return (
            False,
            None,
            "Could not set up background mining at sign-in. Scheduled task failed "
            f"({task_error}); Run entry failed ({error}).",
        )
    _finish_background_registration("run-key")
    return (
        True,
        "run-key",
        "A scheduled task could not be created "
        f"({task_error}), so background mining will start at sign-in from a "
        "per-user Run entry instead.",
    )


def _unregister_task_elevated():
    q = _powershell_single_quoted
    error_path = _elevation_error_path()
    script = (
        "$ErrorActionPreference = 'Stop'; try { "
        f"Unregister-ScheduledTask -TaskName {q(_BACKGROUND_TASK_NAME)} -Confirm:$false; "
        "exit 0 } catch { "
        f"Set-Content -LiteralPath {q(str(error_path))} -Value $_.Exception.Message -Encoding UTF8; "
        "exit 1 }"
    )
    return _run_elevated_powershell(script, error_path)


def unregister_background_task():
    """Stop background mining if it is running, then remove the scheduled task
    and the Run entry. Returns (ok, message)."""
    if not _request_background_stop():
        return (
            False,
            "Background mining is still running and did not stop in time. "
            "Stop it first, then try again.",
        )
    problems = []
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Delete", "/TN", _BACKGROUND_TASK_NAME, "/F"],
            capture_output=True,
            text=True,
            timeout=60,
            **_no_window_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        problems.append(f"could not run schtasks.exe ({error})")
    else:
        # A task that was never created (or already removed) is the goal
        # state, so only other failures count.
        output = f"{result.stdout}\n{result.stderr}".lower()
        if (
            result.returncode != 0
            and "cannot find" not in output
            and "does not exist" not in output
        ):
            reason = _first_line(result.stderr) or result.returncode
            # A boot task registered by an older build, or by an
            # administrator, may only be deletable elevated.
            if "access is denied" in output:
                ok, detail = _unregister_task_elevated()
                if not ok:
                    problems.append(
                        f"could not delete the scheduled task ({reason}; "
                        f"as administrator: {detail})"
                    )
            else:
                problems.append(f"could not delete the scheduled task ({reason})")
    try:
        _delete_background_run_value()
    except (ImportError, OSError) as error:
        problems.append(f"could not remove the Run entry ({error})")
    if problems:
        return (
            False,
            "Could not fully remove background mining: " + "; ".join(problems) + ".",
        )
    return True, "Background mining no longer starts automatically."


def _register_background_main():
    """`--register-background`: installed app only, as the mining-at-sign-in
    setup step. Logs the outcome to background.log."""
    _prepare_application_directories()
    if not getattr(sys, "frozen", False):
        _background_log(
            "Background mode can only be registered from the installed app."
        )
        return 1
    ok, _mechanism, message = register_background_task(
        at_boot=_AT_BOOT_FLAG in sys.argv[1:]
    )
    _background_log(message if ok else f"Background registration failed: {message}")
    return 0 if ok else 1


def _open_sign_in():
    """Start whatever this build uses to sign in: the --setup console for the
    background build, the desktop window otherwise."""
    argv = [sys.executable, _SETUP_FLAG] if _is_headless_build() else [sys.executable]
    try:
        subprocess.Popen(argv, cwd=str(Path(sys.executable).parent))
    except OSError as error:
        _background_log(f"Could not open the sign-in window: {error}")
        return 1
    return 0


def _start_background_main():
    """`--start-background`: start background mining now.

    Runs the scheduled task when there is one, so a boot task's miner keeps
    running after sign-out; otherwise starts the supervisor as a child that
    outlives this process. Opens the sign-in step instead while the username
    or Twitch login is missing.
    """
    _exe_dir, application_dir = _prepare_application_directories()
    if _background_instance_running():
        _background_log("Background mining is already running.")
        return 0
    config_dir, _created = prepare_config(application_dir)
    problem = _background_setup_problem(config_dir / "config.py", application_dir)
    if problem:
        _background_log(problem)
        return _open_sign_in()
    if _background_task_xml() is not None:
        try:
            result = subprocess.run(
                ["schtasks.exe", "/Run", "/TN", _BACKGROUND_TASK_NAME],
                capture_output=True,
                text=True,
                timeout=60,
                **_no_window_flags(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            _background_log(f"Could not run the scheduled task ({error}).")
        else:
            if result.returncode == 0:
                _background_log("Started background mining from its scheduled task.")
                return 0
            _background_log(
                "Could not run the scheduled task "
                f"({_first_line(result.stderr) or result.returncode}); "
                "starting background mining directly."
            )
    try:
        subprocess.Popen(
            [sys.executable, _BACKGROUND_FLAG],
            cwd=str(Path(sys.executable).parent),
            **_no_window_flags(),
        )
    except OSError as error:
        _background_log(f"Could not start background mining: {error}")
        return 1
    return 0


_START_MODE_TEXT = {
    "boot": "It starts when Windows starts, before anyone signs in.",
    "sign-in": "It starts when you sign in.",
    "run-key": "It starts when you sign in (from a Run entry).",
    None: "It is not set to start automatically.",
}


def _background_status_text():
    running = _background_instance_running()
    lines = [
        (
            "Background mining is running."
            if running
            else "Background mining is not running."
        ),
        _START_MODE_TEXT[_background_start_mode()],
        f"Log: {_background_log_path()}",
    ]
    try:
        log_lines = _background_log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        log_lines = []
    last = next((line for line in reversed(log_lines) if line.strip()), "")
    if last:
        lines.append(f"Last entry: {last}")
    return "\n\n".join(lines)


def _background_status_main():
    """`--background-status`: a message box with the background state."""
    _prepare_application_directories()
    _message_box(_background_status_text(), "Twitch Channel Points Miner", 0x40)
    return 0


def _attach_console():
    """Give this windowed exe a console of its own and point stdin, stdout and
    stderr at it, so --setup can show Twitch's activation code and read a
    username. Returns False when there is no console to use."""
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.AllocConsole()  # Fails harmlessly when one is attached.
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
        sys.stdin = open("CONIN$", "r", encoding="utf-8")
    except (AttributeError, OSError):
        return False
    return True


def _prompt_username(config_path):
    """Ask for the Twitch username until a valid one is saved. Returns False
    when input ends first."""
    from TwitchChannelPointsMiner.config_editor import set_miner_username

    while True:
        try:
            username = input("Twitch username: ")
        except (EOFError, RuntimeError):
            return False
        try:
            set_miner_username(config_path, username)
        except ConfigEditError as error:
            print(error)
            continue
        return True


def _sign_in_with_miner(config_dir, exe_dir, config_path, application_dir):
    """Run the miner in this console until its Twitch login is saved, then
    stop it. Returns True once signed in."""
    argv = [
        "--config-dir",
        str(config_dir),
        "--legacy-runner",
        str(exe_dir / "run.py"),
    ]
    handle = _MinerThreadHandle()
    thread = threading.Thread(
        target=_run_miner_thread,
        args=(argv, handle.set_miner),
        name="Miner runner",
        daemon=True,
    )
    handle.thread = thread
    thread.start()
    while True:
        try:
            signed_in = _twitch_session_saved(config_path, application_dir)
        except (OSError, SyntaxError):
            signed_in = False
        if signed_in:
            print("\nSigned in to Twitch.", flush=True)
            _stop_miner_gracefully(handle)
            thread.join(_BACKGROUND_STOP_TIMEOUT)
            return True
        if not thread.is_alive():
            print(
                "\nThe miner stopped before the Twitch sign-in finished. "
                "See the messages above.",
                flush=True,
            )
            return False
        thread.join(_BACKGROUND_POLL_SECONDS)


def _setup_main():
    """`--setup`: the background build's first-run console.

    Asks for the Twitch username if none is set, runs the miner here until
    Twitch's activation finishes, then starts background mining. Also safe
    to run again later: once signed in it only starts background mining.
    """
    _attach_console()
    exe_dir, application_dir = _prepare_application_directories()
    print("Twitch Channel Points Miner - background mining setup\n", flush=True)
    if not _acquire_single_instance_lock():
        if _background_instance_running():
            print(
                "Background mining is already running. To sign in again, "
                'choose "Stop background mining" in the Start menu first.'
            )
        else:
            print("Twitch Channel Points Miner is already running.")
        pause_for_first_run()
        return 0
    if _is_standard_build():
        _migrate_legacy_windows_data(exe_dir, application_dir)
    config_dir, _created = prepare_config(application_dir)
    config_path = config_dir / "config.py"
    if _needs_username(config_path):
        print("Enter the Twitch account to mine with.")
        if not _prompt_username(config_path):
            return 1
    try:
        signed_in = _twitch_session_saved(config_path, application_dir)
    except (OSError, SyntaxError) as error:
        print(f"Could not read {config_path}: {error}")
        pause_for_first_run()
        return 1
    if signed_in:
        print("Already signed in to Twitch.")
    else:
        print(
            "Signing in to Twitch. Open the link below and enter the code it "
            "shows; this window continues by itself afterwards.\n",
            flush=True,
        )
        if not _sign_in_with_miner(config_dir, exe_dir, config_path, application_dir):
            pause_for_first_run()
            return 1
    _release_single_instance_lock()
    print("Starting background mining...", flush=True)
    code = _start_background_main()
    if code == 0:
        print(
            "Background mining is running. You can close this window.\n"
            'Stop it with "Stop background mining" in the Start menu; its log is '
            f"{_background_log_path()}."
        )
    else:
        print(f"Background mining did not start. See {_background_log_path()}.")
    pause_for_first_run()
    return code


def _unregister_background_main():
    """`--unregister-background`: stops background mining if it is running and
    removes its sign-in entry. Logs the outcome to background.log."""
    _prepare_application_directories()
    ok, message = unregister_background_task()
    _background_log(message)
    return 0 if ok else 1


def self_test():
    """Verify the frozen exe actually has its GUI dependency bundled.

    Used only as `--self-test`, wired up as a CI smoke-test step right
    after build_windows.bat: it isolates the one import launch_shell()
    needs (pywebview) without touching config, the miner, or any window,
    so a PyInstaller bundle missing it - e.g. because a build step
    installed requirements.txt instead of requirements-windows.txt -
    fails the build immediately instead of only surfacing for a user at
    runtime. Must never open a window or message box: nothing would be
    there to dismiss it on a CI runner, and the job would hang forever.
    """
    if _is_headless_build():
        # The background build must not carry the window and tray: that is
        # what keeps it small.
        bundled = [
            name
            for name in ("webview", "pystray", "PIL")
            if importlib.util.find_spec(name) is not None
        ]
        if bundled:
            print(f"self-test FAILED: headless build bundles {', '.join(bundled)}")
            return 1
        print("self-test OK: headless build")
        return 0
    try:
        import webview  # noqa: F401
    except Exception as error:
        print(f"self-test FAILED: could not import webview: {error}")
        return 1
    print("self-test OK: webview is importable")
    return 0


def main():
    if "--self-test" in sys.argv[1:]:
        return self_test()

    # Background-mode flags each run their own entry point and return from
    # here, so none of them ever reaches the forwarded runner arguments below.
    launcher_args = sys.argv[1:]
    if _BACKGROUND_FLAG in launcher_args:
        return _background_main()
    if _BACKGROUND_WORKER_FLAG in launcher_args:
        return _end_background_worker(_background_worker_main())
    if _STOP_BACKGROUND_FLAG in launcher_args:
        return _stop_background_main()
    if _REGISTER_BACKGROUND_FLAG in launcher_args:
        return _register_background_main()
    if _UNREGISTER_BACKGROUND_FLAG in launcher_args:
        return _unregister_background_main()
    if _START_BACKGROUND_FLAG in launcher_args:
        return _start_background_main()
    if _BACKGROUND_STATUS_FLAG in launcher_args:
        return _background_status_main()
    # The background build has no window, so any other launch is its setup.
    if _SETUP_FLAG in launcher_args or _is_headless_build():
        # Miner threads that outlive the sign-in must not hold the exit.
        return _end_background_worker(_setup_main())

    # A scripted/automation invocation (e.g. `--convert-only`) never opens a
    # window and does its work and exits - it's fine, and expected (e.g.
    # from an installer step or a script), for that to run alongside an
    # already-running desktop instance, so it's exempted from this check
    # entirely rather than treated as a second launch of the app itself.
    if "--convert-only" not in sys.argv[1:] and not _acquire_single_instance_lock():
        if _background_instance_running():
            # Background mining holds the same lock, so there is no window to
            # bring forward - explain how to get the desktop window instead.
            _message_box(
                "Twitch Channel Points Miner is already running in the background "
                "(background mode).\n\n"
                "To use the desktop window instead, stop background mining from "
                'the Start menu ("Stop background mining") and open the app again.',
                "Twitch Channel Points Miner",
                0x40,  # MB_ICONINFORMATION
            )
            return 0
        _notify_running_instance()
        _message_box(
            "Twitch Channel Points Miner is already running.\n\n"
            "Bringing the existing window to the foreground - check your "
            "taskbar or system tray if it doesn't appear.",
            "Twitch Channel Points Miner",
            0x40,  # MB_ICONINFORMATION
        )
        return 0

    exe_dir, application_dir = _prepare_application_directories()

    console_buffer = ConsoleBuffer()
    install_console_capture(console_buffer)
    _configure_launcher_logging()

    commit_hash = _build_commit_hash()
    if commit_hash:
        # Printed as early as possible so it's the first thing visible in
        # the Console tab regardless of what else this run does - and set
        # as an env var so AnalyticsServer's dashboard footer (running in
        # a package that has no notion of a PyInstaller bundle to read
        # commit_hash.txt from itself) can show it too.
        print(f"Build: {commit_hash}")
        os.environ[BUILD_COMMIT_ENV_VAR] = commit_hash

    # Always attempted for a standard build (never for portable, which has
    # nothing to migrate to), regardless of whether this turns out to be an
    # interactive launch - see the notice-display gating below.
    migration_notice = (
        _migrate_legacy_windows_data(exe_dir, application_dir)
        if _is_standard_build()
        else None
    )

    config_dir, created = prepare_config(application_dir)
    config_path = config_dir / "config.py"

    # A launcher-only flag (set by the "start on Windows login" registry
    # entry / installer shortcut - see _autostart_command) - runner.py's own
    # argument parser knows nothing about it, so it must never reach
    # runner_main().
    start_minimized = _START_MINIMIZED_FLAG in sys.argv[1:]
    forwarded_args = [arg for arg in sys.argv[1:] if arg != _START_MINIMIZED_FLAG]

    argv = [
        "--config-dir",
        str(config_dir),
        "--legacy-runner",
        str(exe_dir / "run.py"),
        *forwarded_args,
    ]
    # A scripted/automation invocation (e.g. `--convert-only`) should behave
    # exactly as before: do the work and exit, with no desktop window - so
    # the migration notice below (a blocking modal) must never show for one.
    interactive = "--convert-only" not in argv

    if migration_notice and interactive:
        _show_migration_notice(migration_notice)

    if created:
        print(f"Created {config_path}")
        if interactive:
            password = ensure_windows_analytics_defaults(
                config_path, just_created=created
            )
            if password:
                print(
                    "Enabled the embedded dashboard for this first run. If "
                    "the desktop window or your browser asks for "
                    "credentials, the username is your Twitch username and "
                    f"the password is: {password}"
                )

    if not interactive:
        return runner_main(argv)

    # A fresh, process-only secret (never written to config.py or disk) so
    # this run's own embedded dashboard can skip the Basic Auth prompt -
    # see _dashboard_url_with_bypass() and AnalyticsServer's
    # require_authentication(). Set before the miner thread starts (whenever
    # that ends up being - see the username-setup deferral below) so it's
    # already there by the time AnalyticsServer reads it.
    os.environ[SHELL_BYPASS_TOKEN_ENV_VAR] = secrets.token_urlsafe(32)

    # Read before starting the miner thread below, which loads (and may
    # migrate/rewrite) the same file - avoids racing two concurrent writers
    # on a first run right after an upgrade.
    dashboard_info = resolve_dashboard_info(config_path)

    miner_thread_handle = _MinerThreadHandle()

    def start_mining():
        thread = threading.Thread(
            target=_run_miner_thread,
            args=(argv, miner_thread_handle.set_miner),
            name="Miner runner",
            daemon=True,
        )
        miner_thread_handle.thread = thread
        thread.start()

    # A config still carrying the bundled template's placeholder username
    # would just fail Twitch login immediately - collect a real one through
    # the shell's setup panel first, and defer starting the miner until
    # then, instead of starting it now knowing it can only fail.
    needs_username = _needs_username(config_path)
    if not needs_username:
        start_mining()

    # Tied to a marker file rather than `created`, so the Windows installer's
    # own pre-created config.py (see windows_installer.iss) still gets the
    # onboarding view on its actual first launch of the exe.
    onboarding_marker = config_dir / ".desktop_shell_onboarded"
    is_first_shell_launch = not onboarding_marker.is_file()
    initial_tab = "config" if is_first_shell_launch else None
    # Separate marker: tracks the one-time "enable the dashboard?" consent
    # prompt for an existing config with analytics off, independent of the
    # onboarding view above (see _maybe_prompt_to_enable_analytics).
    analytics_prompt_marker = config_dir / ".shell_analytics_prompt_shown"

    try:
        launch_shell(
            dashboard_info,
            console_buffer,
            initial_tab,
            miner_thread_handle,
            config_path,
            analytics_prompt_marker,
            needs_username,
            start_mining,
            application_dir / "logs",
            # First-run setup must still show the window regardless of how
            # it was launched - there would be nothing to interact with.
            start_minimized=start_minimized and not needs_username,
        )
    except Exception as error:
        # Covers a missing pywebview install, no WebView2 runtime, or any
        # other GUI backend failure - none of which should crash the miner.
        message = (
            f"Could not open the desktop window ({error}); "
            "falling back to your default browser."
        )
        print(message)
        _show_fatal_error_message(message)
        if needs_username:
            # No dashboard exists to fall back to, and mining never started -
            # the only way forward left is editing the file directly.
            print(
                "No Twitch username is configured yet. Set 'username' under "
                f"MINER_CONFIG in {config_path} and restart."
            )
        elif dashboard_info:
            webbrowser.open(_dashboard_url_with_bypass(dashboard_info["url"]))
        if miner_thread_handle.thread is not None:
            # There is no GUI event loop to keep the process alive now.
            # Wait for mining to end, including in a windowed build with no stdin.
            miner_thread_handle.thread.join()
        pause_for_first_run()
    else:
        if is_first_shell_launch:
            try:
                onboarding_marker.touch()
            except OSError:
                pass
    finally:
        if miner_thread_handle.thread is not None:
            miner_thread_handle.thread.join(timeout=2)

    return 0


def run():
    """Entry point wrapper: on a genuinely uncaught failure, show a native
    message box before re-raising.

    A --windowed build has no console to print a traceback to, so without
    this an unexpected crash here would fail completely silently - no
    window, no console, no error, nothing.
    """
    try:
        return main()
    except Exception as error:
        _show_fatal_error_message(
            "Twitch Channel Points Miner failed to start:\n\n"
            f"{error}\n\n"
            "Check the logs folder beside the executable for details."
        )
        raise


if __name__ == "__main__":
    raise SystemExit(run())
