"""Claude Desktop MCP setup utility.

A single-window Tkinter tool that finds (or installs) Claude Desktop on
Windows and registers an MCP server in
``%APPDATA%\\Claude\\claude_desktop_config.json``.

Nothing about the host machine is hard-coded: install locations, the config
file and the Python interpreter are all resolved at runtime from environment
variables, the registry, the ``py`` launcher and ``PATH``. Only the standard
library is used, so it runs either as a script or as a frozen bundle::

    python utils/claude_setup.py
    pyinstaller --onefile --noconsole utils/claude_setup.py
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import winreg
else:  # importable elsewhere so the window can explain why it is disabled
    winreg = None  # type: ignore[assignment]

APP_NAME = "Claude Setup Utility"
APP_VERSION = "2.0"

# Claude Desktop's own download page, both as the human fallback and as the
# place the installer links are scraped from when the endpoints below move.
CLAUDE_DOWNLOAD_PAGE = "https://claude.ai/download"

# Anthropic's official per-architecture endpoints. Each answers with a redirect
# to the current signed installer on downloads.claude.ai, so the version is
# never pinned here.
CLAUDE_INSTALLERS = {
    "x64": "https://claude.ai/api/desktop/win32/x64/setup/latest/redirect",
    "arm64": "https://claude.ai/api/desktop/win32/arm64/setup/latest/redirect",
}

# These endpoints answer 403 to a request with no User-Agent, which is what
# urllib sends by default, so one is always set.
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (Windows)"

DOWNLOAD_CHUNK = 256 * 1024
DOWNLOAD_TIMEOUT = 60
PROBE_TIMEOUT = 10

# MCP server keys land in JSON and in command lines, so keep them boring.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

INTERPRETER_FILETYPES = [
    ("Python interpreter", "python.exe;pythonw.exe;python3.exe"),
    ("Executables", "*.exe"),
    ("All files", "*.*"),
]

SERVER_FILETYPES = [
    ("MCP server", "*.py;*.pyw;*.exe;*.cmd;*.bat;*.js;*.mjs"),
    ("Python script", "*.py;*.pyw"),
    ("Executable", "*.exe;*.cmd;*.bat"),
    ("Node script", "*.js;*.mjs"),
    ("All files", "*.*"),
]

# ============================================================
# Platform helpers
# ============================================================

def run_hidden(command: list[str], timeout: float = PROBE_TIMEOUT) -> str:
    """Run a console command without flashing a window and return its output.

    Args:
        command: Argument vector; never a shell string.
        timeout: Seconds to wait before giving up.

    Returns:
        str: Combined stdout and stderr, or an empty string on any failure.
    """
    startupinfo = None
    creationflags = 0

    if IS_WINDOWS:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            startupinfo=startupinfo,
            creationflags=creationflags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    return f"{completed.stdout}{completed.stderr}".strip()


def machine_arch() -> str:
    """Identify the Windows architecture used to pick an installer.

    Returns:
        str: ``"arm64"`` on ARM hardware, otherwise ``"x64"``.
    """
    raw = " ".join(
        value
        for value in (
            os.environ.get("PROCESSOR_ARCHITECTURE", ""),
            os.environ.get("PROCESSOR_ARCHITEW6432", ""),
            platform.machine(),
        )
        if value
    ).lower()

    return "arm64" if "arm" in raw or "aarch64" in raw else "x64"


def roaming_dir() -> Path:
    """Locate the roaming application data directory.

    ``APPDATA`` is normally set, but it is missing from some service and
    scheduled-task environments, so the user profile is used as a fallback.

    Returns:
        Path: Directory that holds per-user roaming configuration.
    """
    appdata = os.environ.get("APPDATA")

    if appdata:
        return Path(appdata)

    return Path.home() / "AppData" / "Roaming"


def local_dir() -> Path:
    """Locate the local (non-roaming) application data directory.

    Returns:
        Path: Directory that holds per-user local application data.
    """
    localappdata = os.environ.get("LOCALAPPDATA")

    if localappdata:
        return Path(localappdata)

    return Path.home() / "AppData" / "Local"


def packaged_data_dirs() -> list[Path]:
    """List the per-user data directories of packaged Claude installations.

    Claude Desktop is published both as a classic installer and as a packaged
    (Microsoft Store / MSIX) app. The documented configuration location is
    ``%APPDATA%\\Claude\\claude_desktop_config.json``, but a packaged app's
    ``%APPDATA%`` writes are redirected by Windows into its own container under
    ``%LOCALAPPDATA%\\Packages\\<package family>\\LocalCache\\Roaming``. This
    tool runs outside that container, so it has to address the redirected copy
    directly.

    Returns:
        list[Path]: Matching ``Packages\\Claude_*`` directories, sorted by name;
        empty when no packaged installation is present.
    """
    try:
        return sorted(
            item
            for item in (local_dir() / "Packages").glob("Claude_*")
            if item.is_dir()
        )
    except OSError:
        return []


def classic_config() -> Path:
    """Build the documented configuration path for a classic installation.

    Returns:
        Path: ``%APPDATA%\\Claude\\claude_desktop_config.json``.
    """
    return roaming_dir() / "Claude" / "claude_desktop_config.json"


def config_candidates() -> list[Path]:
    """List every configuration path Claude Desktop might be reading.

    Container paths come first when a packaged installation has run on this
    machine, because a packaged Claude never sees the plain ``%APPDATA%`` copy.

    Returns:
        list[Path]: Candidate configuration files, most likely first.
    """
    candidates = [
        directory / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
        for directory in packaged_data_dirs()
    ]
    candidates.append(classic_config())

    return candidates


def _server_count(path: Path) -> int:
    """Count the MCP servers a configuration file already declares.

    Args:
        path: Candidate configuration file.

    Returns:
        int: Number of entries, or 0 when the file is absent or unreadable.
    """
    try:
        return len(list_servers(load_config(path)))
    except ConfigError:
        return 0


def config_path() -> Path:
    """Resolve the configuration file Claude Desktop actually uses.

    Both locations can exist at once — an empty ``%APPDATA%`` file is easy to
    end up with after switching installers — so a candidate that already
    declares servers wins over one that is merely present, and file
    modification times are deliberately not used as the tiebreaker.

    Returns:
        Path: Path to ``claude_desktop_config.json``, existing or not.
    """
    candidates = config_candidates()

    for candidate in candidates:
        if _server_count(candidate) > 0:
            return candidate

    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue

    return candidates[0]


def download_dir() -> Path:
    """Pick a writable directory for the downloaded installer.

    Returns:
        Path: Temporary directory dedicated to this tool.
    """
    return Path(tempfile.gettempdir()) / "ClaudeMCPSetup"


def human_size(count: int | float) -> str:
    """Format a byte count for display.

    Args:
        count: Number of bytes.

    Returns:
        str: Size with a binary unit suffix, for example ``"12.4 MB"``.
    """
    size = float(count)

    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024

    return f"{size:.1f} GB"


def split_arguments(text: str) -> list[str]:
    """Split a command-line string into arguments, Windows-style.

    ``shlex`` in POSIX mode treats the backslash as an escape character, which
    destroys Windows paths, so non-POSIX mode is used and the quote characters
    it leaves behind are stripped afterwards.

    Args:
        text: Raw argument string entered by the user.

    Returns:
        list[str]: Individual arguments with surrounding quotes removed.

    Raises:
        ValueError: If the string has an unbalanced quote.
    """
    tokens = shlex.split(text, posix=False)
    arguments = []

    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        arguments.append(token)

    return arguments


# ============================================================
# Claude Desktop detection
# ============================================================

# Claude Desktop is a Squirrel app: it installs per-user under LOCALAPPDATA and
# registers an uninstall key. The registry is authoritative because it records
# wherever the user actually installed it; the path list below only covers the
# defaults for the case where the key is missing or stale.
UNINSTALL_KEYS = (
    (
        "HKCU",
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Claude",
    ),
    (
        "HKLM",
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Claude",
    ),
    (
        "HKLM",
        r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion"
        r"\Uninstall\Claude",
    ),
)


def _read_registry_value(root_name: str, subkey: str, value: str) -> str | None:
    """Read one registry string value, tolerating every absence.

    Args:
        root_name: Either ``"HKCU"`` or ``"HKLM"``.
        subkey: Key path below the root.
        value: Value name to read.

    Returns:
        str | None: The stored string, or None when unavailable.
    """
    if winreg is None:
        return None

    root = winreg.HKEY_CURRENT_USER
    if root_name == "HKLM":
        root = winreg.HKEY_LOCAL_MACHINE

    try:
        with winreg.OpenKey(root, subkey) as key:
            data, _ = winreg.QueryValueEx(key, value)
    except OSError:
        return None

    return data if isinstance(data, str) and data else None


def _candidate_paths() -> list[Path]:
    """Build the default Claude Desktop executable locations.

    Returns:
        list[Path]: Candidate paths in order of likelihood.
    """
    local = local_dir()
    roots = [
        local / "AnthropicClaude",
        local / "Programs" / "Claude",
        local / "Claude",
    ]

    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
        base = os.environ.get(variable)
        if base:
            roots.append(Path(base) / "Claude")

    candidates: list[Path] = []

    for root in roots:
        candidates.append(root / "Claude.exe")
        candidates.append(root / "claude.exe")

    return candidates


def _newest_versioned_exe(root: Path) -> Path | None:
    """Find the executable inside the newest Squirrel ``app-*`` folder.

    Squirrel keeps each installed version in its own ``app-<version>``
    directory next to a launcher stub, so the highest version wins.

    Args:
        root: Installation root to search.

    Returns:
        Path | None: Executable path, or None when nothing matches.
    """
    if not root.is_dir():
        return None

    def version_key(folder: Path) -> tuple[int, ...]:
        parts = folder.name[4:].split(".")
        numbers = []
        for part in parts:
            digits = "".join(character for character in part if character.isdigit())
            numbers.append(int(digits) if digits else 0)
        return tuple(numbers)

    try:
        versions = sorted(
            (
                item
                for item in root.iterdir()
                if item.is_dir() and item.name.startswith("app-")
            ),
            key=version_key,
            reverse=True,
        )
    except OSError:
        return None

    for folder in versions:
        for name in ("Claude.exe", "claude.exe"):
            candidate = folder / name
            if candidate.is_file():
                return candidate

    return None


# Packaged (Microsoft Store) installations are not listed under Uninstall. They
# are registered here, per user, with the root folder recorded as a value.
PACKAGE_REPOSITORY = (
    r"Software\Classes\Local Settings\Software\Microsoft\Windows"
    r"\CurrentVersion\AppModel\Repository\Packages"
)


def _packaged_roots() -> list[Path]:
    """List the install roots of packaged Claude Desktop versions.

    ``Program Files\\WindowsApps`` cannot be enumerated without elevation, so
    the roots are read from the per-user package repository instead. Individual
    files inside a root are still readable, so probing a known path works.

    Returns:
        list[Path]: Existing roots, ordered oldest package name first.
    """
    if winreg is None:
        return []

    roots: list[tuple[str, Path]] = []

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PACKAGE_REPOSITORY) as key:
            count = winreg.QueryInfoKey(key)[0]

            for index in range(count):
                name = winreg.EnumKey(key, index)

                if not name.lower().startswith("claude_"):
                    continue

                try:
                    with winreg.OpenKey(key, name) as package:
                        root, _ = winreg.QueryValueEx(package, "PackageRootFolder")
                except OSError:
                    continue

                if root:
                    roots.append((name, Path(root)))
    except OSError:
        return []

    return [path for _, path in sorted(roots)]


def find_packaged_claude() -> Path | None:
    """Locate a packaged (Microsoft Store) Claude Desktop installation.

    Returns:
        Path | None: Executable path, or None when no packaged install is found.
    """
    for root in reversed(_packaged_roots()):
        for relative in ("app/claude.exe", "claude.exe", "app/Claude.exe"):
            candidate = root / relative
            if candidate.is_file():
                return candidate

    return None


def find_running_claude() -> Path | None:
    """Read the executable path of a running Claude Desktop process.

    Last-resort detection: it reports the installation actually in use, and it
    still works for install layouts none of the other probes know about.

    Returns:
        Path | None: Executable path, or None when nothing is running.
    """
    if not IS_WINDOWS:
        return None

    output = run_hidden(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-Process claude -ErrorAction SilentlyContinue | "
            "Select-Object -First 1).Path",
        ],
        timeout=20,
    )

    for line in output.splitlines():
        candidate = Path(line.strip().strip('"'))

        if candidate.name.lower() == "claude.exe" and candidate.is_file():
            return candidate

    return None


def find_claude() -> Path | None:
    """Locate the installed Claude Desktop executable.

    Checks the uninstall registry entries first, then the default install
    directories, then Squirrel's per-version subfolders, then a packaged
    Microsoft Store installation, and finally a running process.

    Returns:
        Path | None: Executable path, or None when Claude is not installed.
    """
    for root_name, subkey in UNINSTALL_KEYS:
        for value in ("DisplayIcon", "InstallLocation", "UninstallString"):
            raw = _read_registry_value(root_name, subkey, value)
            if not raw:
                continue

            # DisplayIcon may carry an icon index; UninstallString may be quoted.
            cleaned = raw.split(",")[0].strip().strip('"')
            if not cleaned:
                continue

            target = Path(os.path.expandvars(cleaned))

            if target.is_file() and target.suffix.lower() == ".exe":
                if "uninstall" not in target.name.lower():
                    return target
                target = target.parent

            if target.is_dir():
                for name in ("Claude.exe", "claude.exe"):
                    direct = target / name
                    if direct.is_file():
                        return direct

                nested = _newest_versioned_exe(target)
                if nested is not None:
                    return nested

    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate

    for root in (
        local_dir() / "AnthropicClaude",
        local_dir() / "Programs" / "Claude",
    ):
        nested = _newest_versioned_exe(root)
        if nested is not None:
            return nested

    packaged = find_packaged_claude()

    if packaged is not None:
        return packaged

    return find_running_claude()


def claude_version(executable: Path) -> str | None:
    """Read Claude Desktop's version without launching it.

    Args:
        executable: Path to the Claude executable.

    Returns:
        str | None: Version string, or None when it cannot be determined.
    """
    # Squirrel: ...\app-1.2.3\Claude.exe. MSIX: ...\Claude_1.2.3.0_x64__abc\app\claude.exe.
    for pattern in (r"app-([0-9][0-9.]*)", r"Claude_([0-9][0-9.]*)_"):
        match = re.search(pattern, str(executable))
        if match:
            return match.group(1).rstrip(".")

    for root_name, subkey in UNINSTALL_KEYS:
        version = _read_registry_value(root_name, subkey, "DisplayVersion")
        if version:
            return version

    return None


def claude_is_running() -> bool:
    """Report whether a Claude Desktop process is currently active.

    A running instance keeps the old configuration in memory, so the user is
    told to restart it after the config file changes.

    Returns:
        bool: True when at least one ``claude.exe`` process is listed.
    """
    if not IS_WINDOWS:
        return False

    output = run_hidden(
        ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/NH"],
    )

    return "claude.exe" in output.lower()


# ============================================================
# Python interpreter discovery
# ============================================================

# Claude Desktop spawns MCP servers with a minimal environment, so a bare
# "python" command often fails even when it works in a terminal. An absolute
# interpreter path is written into the config instead. sys.executable is only
# usable when this tool runs as a script — under PyInstaller it points at the
# frozen bundle, so the interpreter is discovered from the system.
FROZEN = getattr(sys, "frozen", False)


def _registry_interpreters() -> list[Path]:
    """List interpreters registered under the PEP 514 registry keys.

    Returns:
        list[Path]: Interpreter paths, newest version first.
    """
    if winreg is None:
        return []

    found: list[tuple[str, Path]] = []
    roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Python"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Python"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Python"),
    )

    for root, base in roots:
        try:
            with winreg.OpenKey(root, base) as company_key:
                company_count = winreg.QueryInfoKey(company_key)[0]

                for company_index in range(company_count):
                    company = winreg.EnumKey(company_key, company_index)

                    try:
                        with winreg.OpenKey(company_key, company) as tag_key:
                            tag_count = winreg.QueryInfoKey(tag_key)[0]

                            for tag_index in range(tag_count):
                                tag = winreg.EnumKey(tag_key, tag_index)

                                try:
                                    with winreg.OpenKey(
                                        tag_key, rf"{tag}\InstallPath"
                                    ) as path_key:
                                        install, _ = winreg.QueryValueEx(
                                            path_key, ""
                                        )
                                except OSError:
                                    continue

                                if not install:
                                    continue

                                executable = Path(install) / "python.exe"
                                if executable.is_file():
                                    found.append((tag, executable))
                    except OSError:
                        continue
        except OSError:
            continue

    def sort_key(item: tuple[str, Path]) -> tuple[int, int]:
        parts = item[0].split("-")[0].split(".")
        try:
            return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except ValueError:
            return (0, 0)

    return [path for _, path in sorted(found, key=sort_key, reverse=True)]


def _launcher_interpreters() -> list[Path]:
    """List interpreters reported by the ``py`` launcher.

    Returns:
        list[Path]: Interpreter paths in the launcher's own order.
    """
    output = run_hidden(["py", "-0p"])
    if not output:
        return []

    paths: list[Path] = []

    for line in output.splitlines():
        # Lines look like: " -V:3.12 *        C:\Path\python.exe"
        match = re.search(r"([A-Za-z]:\\[^\r\n]*python(?:w)?\.exe)", line)
        if match:
            candidate = Path(match.group(1))
            if candidate.is_file():
                paths.append(candidate)

    return paths


def console_interpreter(interpreter: Path) -> Path:
    """Map a windowed interpreter to its console counterpart.

    MCP servers talk over stdio, and ``pythonw.exe`` starts with no standard
    streams attached, so a server launched with it never answers. This tool may
    itself be running under ``pythonw``, which is how that path would otherwise
    end up in the configuration.

    Args:
        interpreter: Interpreter path to normalise.

    Returns:
        Path: ``python.exe`` beside the given path when it exists, else the
        path unchanged.
    """
    if interpreter.name.lower() != "pythonw.exe":
        return interpreter

    console = interpreter.with_name("python.exe")

    return console if console.is_file() else interpreter


def find_python() -> Path | None:
    """Resolve an absolute Python interpreter suitable for MCP servers.

    Returns:
        Path | None: Interpreter path, or None when none was found.
    """
    candidates: list[Path] = []

    if not FROZEN:
        candidates.append(Path(sys.executable))

    candidates.extend(_launcher_interpreters())
    candidates.extend(_registry_interpreters())

    for name in ("python", "python3"):
        located = shutil.which(name)
        if located:
            candidates.append(Path(located))

    seen: set[str] = set()

    for candidate in candidates:
        candidate = console_interpreter(candidate)
        key = str(candidate).lower()

        if key in seen or not candidate.is_file():
            continue

        seen.add(key)

        # WindowsApps ships an App Execution Alias stub that opens the Store
        # instead of running Python; skip it.
        if "windowsapps" in key:
            continue

        return candidate

    return None


def python_label(interpreter: Path) -> str:
    """Describe an interpreter for the status line.

    Args:
        interpreter: Interpreter to query.

    Returns:
        str: Version and path, or just the path when the probe fails.
    """
    output = run_hidden([str(interpreter), "--version"])
    version = output.splitlines()[0].strip() if output else ""

    return f"{version} — {interpreter}" if version else str(interpreter)


# ============================================================
# MCP configuration
# ============================================================

class ConfigError(Exception):
    """Raised when the configuration file cannot be read or written."""


def build_entry(
    target: Path,
    interpreter: Path | None,
    extra_args: str,
) -> dict:
    """Build the ``mcpServers`` entry for a server file.

    Two kinds of entry point are the common cases and both are supported:

    - A Python script (``.py``, ``.pyw``) runs under an absolute interpreter
      path, with the script as the first argument. The path is absolute because
      Claude Desktop starts servers with a minimal environment in which a bare
      ``python`` frequently does not resolve.
    - A standalone executable (``.exe``, ``.cmd``, ``.bat``, ``.com``) is the
      command itself, with no interpreter involved.

    Node scripts are handled too, for servers distributed that way.

    Args:
        target: Server entry point on disk.
        interpreter: Interpreter to use for Python scripts.
        extra_args: Additional arguments as a command-line string.

    Returns:
        dict: Entry with ``command`` and ``args`` keys, as Claude Desktop
        expects under ``mcpServers``.

    Raises:
        ConfigError: If a Python script is given with no interpreter, if Node is
            needed but missing, if the file type is unsupported, or if
            ``extra_args`` cannot be parsed.
    """
    suffix = target.suffix.lower()

    if suffix in (".py", ".pyw"):
        if interpreter is None:
            raise ConfigError(
                "No Python interpreter was found, so a Python server cannot be "
                "launched. Install Python 3.10 or newer, or choose an "
                "interpreter with the button above."
            )

        command = str(console_interpreter(interpreter))
        args = [str(target)]

    elif suffix in (".exe", ".cmd", ".bat", ".com"):
        command = str(target)
        args = []

    elif suffix in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")

        if node is None:
            raise ConfigError(
                "This server is a Node script but node.exe was not found on "
                "PATH. Install Node.js, or point at an executable instead."
            )

        command = node
        args = [str(target)]

    else:
        raise ConfigError(
            f"Unsupported server type '{target.suffix or target.name}'. Choose "
            f"a Python script (.py) or an executable (.exe, .cmd, .bat)."
        )

    if extra_args.strip():
        try:
            args.extend(split_arguments(extra_args))
        except ValueError as error:
            raise ConfigError(
                f"Extra arguments could not be parsed: {error}"
            ) from error

    return {"command": command, "args": args}


def load_config(path: Path) -> dict:
    """Read the Claude configuration file.

    A missing file is treated as an empty configuration. A file that exists but
    is not valid JSON is an error rather than something to silently overwrite,
    since it may hold servers the user configured by hand.

    Args:
        path: Configuration file path.

    Returns:
        dict: Parsed configuration, or an empty dict when the file is absent.

    Raises:
        ConfigError: If the file is unreadable or not a JSON object.
    """
    if not path.is_file():
        return {}

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ConfigError(f"Could not read {path.name}: {error}") from error

    if not text.strip():
        return {}

    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"{path.name} is not valid JSON (line {error.lineno}, column "
            f"{error.colno}: {error.msg}). Fix or remove the file before "
            f"adding a server, so existing entries are not lost."
        ) from error

    if not isinstance(data, dict):
        raise ConfigError(
            f"{path.name} must contain a JSON object at the top level, "
            f"found {type(data).__name__}."
        )

    return data


def backup_config(path: Path) -> Path | None:
    """Copy the configuration file aside before it is modified.

    Args:
        path: Configuration file path.

    Returns:
        Path | None: Backup path, or None when there was nothing to back up.

    Raises:
        ConfigError: If the copy fails.
    """
    if not path.is_file():
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.{stamp}.bak.json")

    try:
        shutil.copy2(path, backup)
    except OSError as error:
        raise ConfigError(f"Could not create a backup: {error}") from error

    return backup


def save_config(path: Path, config: dict) -> None:
    """Write the configuration atomically.

    The new content goes to a temporary file in the same directory and is then
    moved into place, so an interrupted write cannot leave Claude with a
    truncated config.

    Args:
        path: Configuration file path.
        config: Configuration to serialise.

    Raises:
        ConfigError: If the directory or file cannot be written.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigError(
            f"Could not create {path.parent}: {error}"
        ) from error

    temporary = path.with_name(f"{path.name}.tmp")

    try:
        temporary.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ConfigError(f"Could not write {path.name}: {error}") from error


def list_servers(config: dict) -> dict:
    """Extract the configured MCP servers.

    Args:
        config: Parsed configuration.

    Returns:
        dict: The ``mcpServers`` mapping, empty when absent or malformed.
    """
    servers = config.get("mcpServers")

    return servers if isinstance(servers, dict) else {}


def describe_entry(entry: object) -> str:
    """Render a server entry as a single readable command line.

    Args:
        entry: Value stored under one ``mcpServers`` key.

    Returns:
        str: Command with its arguments, or a placeholder when malformed.
    """
    if not isinstance(entry, dict):
        return "(invalid entry)"

    parts = [str(entry.get("command", "?"))]
    args = entry.get("args")

    if isinstance(args, list):
        parts.extend(str(item) for item in args)

    return " ".join(f'"{part}"' if " " in part else part for part in parts)


# ============================================================
# Installer download
# ============================================================

class DownloadCancelled(Exception):
    """Raised when the user cancels an in-progress download."""


def open_url(url: str, timeout: float = DOWNLOAD_TIMEOUT):
    """Open a URL with the headers Anthropic's endpoints expect.

    Args:
        url: URL to fetch.
        timeout: Seconds to wait for the response.

    Returns:
        http.client.HTTPResponse: The open response.

    Raises:
        urllib.error.URLError: If the request fails.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        },
    )

    return urllib.request.urlopen(request, timeout=timeout)


def scrape_installer_urls() -> dict[str, str]:
    """Read the current Windows installer links off the download page.

    The fixed endpoints in ``CLAUDE_INSTALLERS`` have changed host before, so
    when one of them fails the links are taken from the page itself rather than
    leaving the user with nothing.

    Returns:
        dict[str, str]: Architecture to URL, empty when nothing was found.
    """
    try:
        with open_url(CLAUDE_DOWNLOAD_PAGE, timeout=PROBE_TIMEOUT * 3) as response:
            html = response.read(2_000_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return {}

    found: dict[str, str] = {}

    for arch in ("x64", "arm64"):
        # Endpoint form: /api/desktop/win32/<arch>/setup/latest/redirect
        match = re.search(
            rf"https://[^\s\"'<>]+/desktop/win32/{arch}/[^\s\"'<>]+",
            html,
        )

        if match:
            found[arch] = match.group(0)
            continue

        # Or a direct .exe link, if the page ever publishes one again.
        direct = re.search(
            rf"https://[^\s\"'<>]*{arch}[^\s\"'<>]*\.exe",
            html,
            re.IGNORECASE,
        )

        if direct:
            found[arch] = direct.group(0)

    return found


def installer_urls(arch: str) -> list[str]:
    """Build the list of installer URLs to try, in order.

    Args:
        arch: Either ``"x64"`` or ``"arm64"``.

    Returns:
        list[str]: The known endpoint first, then whatever the download page
        currently advertises for the same architecture. The architecture is
        never substituted — installing the wrong build is worse than failing.
    """
    urls = [CLAUDE_INSTALLERS[arch]]
    scraped = scrape_installer_urls().get(arch)

    if scraped and scraped not in urls:
        urls.append(scraped)

    return urls


def download_installer(
    url: str,
    destination: Path,
    on_progress,
    should_cancel,
) -> Path:
    """Download the Claude Desktop installer to disk.

    Written in chunks so progress can be reported and cancellation honoured,
    and to a ``.part`` file so an interrupted transfer is never mistaken for a
    complete installer. The result is checked for the ``MZ`` executable magic,
    because a redirect that lands on an HTML error page still returns 200.

    Args:
        url: Installer URL.
        destination: Final path for the downloaded file.
        on_progress: Callback receiving ``(downloaded_bytes, total_bytes)``;
            ``total_bytes`` is 0 when the server sends no length.
        should_cancel: Callback returning True when the transfer should stop.

    Returns:
        Path: The completed installer path.

    Raises:
        DownloadCancelled: If ``should_cancel`` returned True.
        OSError: If the transfer fails, the disk write fails, or the downloaded
            file is not a Windows executable.
        urllib.error.URLError: If the request itself fails.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)

    downloaded = 0
    magic = b""

    with open_url(url) as response:
        total = int(response.headers.get("Content-Length") or 0)
        on_progress(0, total)

        with partial.open("wb") as handle:
            while True:
                if should_cancel():
                    handle.close()
                    partial.unlink(missing_ok=True)
                    raise DownloadCancelled

                chunk = response.read(DOWNLOAD_CHUNK)

                if not chunk:
                    break

                if not magic:
                    magic = chunk[:2]

                handle.write(chunk)
                downloaded += len(chunk)
                on_progress(downloaded, total)

    if downloaded == 0:
        partial.unlink(missing_ok=True)
        raise OSError("The server returned an empty response.")

    if magic != b"MZ":
        partial.unlink(missing_ok=True)
        raise OSError(
            "The download did not return a Windows installer. The download "
            "link has probably changed."
        )

    destination.unlink(missing_ok=True)
    os.replace(partial, destination)

    return destination


def launch_installer(installer: Path) -> None:
    """Start the downloaded installer.

    ``os.startfile`` is used so the shell applies the same UAC handling a
    double-click would, and no console window appears.

    Args:
        installer: Installer executable path.

    Raises:
        OSError: If the installer cannot be started.
    """
    if IS_WINDOWS:
        os.startfile(str(installer))  # noqa: S606 - user-initiated install
    else:
        subprocess.Popen([str(installer)])


# ============================================================
# User interface
# ============================================================

def window_icon() -> Path | None:
    """Locate the ``icon.ico`` file that ships beside this program.

    As a script the icon sits in this file's directory; a frozen bundle has
    PyInstaller unpack its data files into ``sys._MEIPASS``.

    Returns:
        Path | None: Icon path, or None when the file is not present.
    """
    if FROZEN:
        extracted = getattr(sys, "_MEIPASS", "")
        base = Path(extracted) if extracted else Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent

    candidate = base / "icon.ico"

    return candidate if candidate.is_file() else None


PALETTE = {
    "bg": "#f4f5f7",
    "card": "#ffffff",
    "text": "#1f2328",
    "muted": "#6a737d",
    "accent": "#c15f3c",
    "ok": "#1a7f37",
    "warn": "#9a6700",
    "error": "#b42318",
}


class SetupWindow:
    """The application window and every action it can perform."""

    def __init__(self, root: tk.Tk) -> None:
        """Build the window and start the first environment check.

        Args:
            root: The Tk root window.
        """
        self.root = root
        self.config_file = config_path()
        self.claude_path: Path | None = None
        self.python_path: Path | None = None

        self.cancel_flag = threading.Event()
        self.busy = False

        self.wrap_labels: list[tuple[ttk.Label, int]] = []
        self.last_width = 0

        self.root.title(APP_NAME)
        self.root.configure(bg=PALETTE["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        icon = window_icon()

        if icon is not None:
            try:
                self.root.iconbitmap(str(icon))
            except tk.TclError:
                # An unreadable icon (corrupt file, .ico on X11) must not stop
                # the window from opening; the default icon is fine.
                pass

        self._build_style()
        self._build_layout()

        self.root.minsize(860, 620)
        self._center(940, 760)
        self.root.bind("<Configure>", self._on_resize)

        self.log(f"{APP_NAME} started.", "info")

        if not IS_WINDOWS:
            self.log(
                "This tool configures Claude Desktop on Windows; detection and "
                "installation are unavailable on this platform.",
                "warn",
            )

        self.refresh_environment()

    # ---------- construction ----------

    def _build_style(self) -> None:
        """Apply a flat light theme to the ttk widgets."""
        style = ttk.Style(self.root)

        for theme in ("vista", "winnative", "clam", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        base = ("Segoe UI", 10) if IS_WINDOWS else ("Helvetica", 11)
        bold = (base[0], base[1], "bold")

        style.configure("TFrame", background=PALETTE["bg"])
        style.configure("Card.TFrame", background=PALETTE["card"])
        style.configure(
            "TLabel",
            background=PALETTE["bg"],
            foreground=PALETTE["text"],
            font=base,
        )
        style.configure("Card.TLabel", background=PALETTE["card"])
        style.configure(
            "Title.TLabel",
            background=PALETTE["bg"],
            foreground=PALETTE["text"],
            font=(base[0], 17, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=PALETTE["bg"],
            foreground=PALETTE["muted"],
            font=base,
        )
        style.configure(
            "Heading.TLabel",
            background=PALETTE["card"],
            foreground=PALETTE["text"],
            font=bold,
        )
        style.configure(
            "Hint.TLabel",
            background=PALETTE["card"],
            foreground=PALETTE["muted"],
            font=(base[0], 9),
        )
        style.configure(
            "Credit.TLabel",
            background=PALETTE["bg"],
            foreground=PALETTE["muted"],
            font=(base[0], 8),
        )
        style.configure(
            "Ok.TLabel",
            background=PALETTE["card"],
            foreground=PALETTE["ok"],
            font=base,
        )
        style.configure(
            "Warn.TLabel",
            background=PALETTE["card"],
            foreground=PALETTE["warn"],
            font=base,
        )
        style.configure(
            "Error.TLabel",
            background=PALETTE["card"],
            foreground=PALETTE["error"],
            font=base,
        )
        style.configure("TButton", font=base, padding=(12, 6))
        style.configure("Accent.TButton", font=bold, padding=(14, 7))
        style.configure("TEntry", padding=4)
        style.configure("TCombobox", padding=4)
        style.configure(
            "TNotebook",
            background=PALETTE["bg"],
            borderwidth=0,
            tabmargins=(2, 6, 2, 0),
        )
        style.configure("TNotebook.Tab", font=base, padding=(16, 8))
        style.configure("Thin.Horizontal.TProgressbar", thickness=8)

    def _card(
        self,
        parent: tk.Widget,
        title: str,
        hint: str = "",
        expand: bool = False,
    ) -> ttk.Frame:
        """Create a titled white panel and return its content frame.

        Args:
            parent: Container to pack the card into.
            title: Heading text.
            hint: Optional muted explanation under the heading.
            expand: Whether the card should absorb leftover vertical space.

        Returns:
            ttk.Frame: Frame that callers place their widgets in.
        """
        outer = ttk.Frame(parent, style="Card.TFrame", padding=14)
        outer.pack(fill="both" if expand else "x", expand=expand, pady=(0, 12))

        ttk.Label(outer, text=title, style="Heading.TLabel").pack(anchor="w")

        if hint:
            self._wrapping(
                ttk.Label(outer, text=hint, style="Hint.TLabel", justify="left"),
                margin=90,
            ).pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(outer, style="Card.TFrame")
        body.pack(fill="both" if expand else "x", expand=expand, pady=(10, 0))

        return body

    def _wrapping(self, label: ttk.Label, margin: int) -> ttk.Label:
        """Register a label whose wrap width follows the window width.

        Long paths and error messages are the main thing this window displays,
        and a fixed ``wraplength`` either clips them or forces the window wider
        than the screen, so it is recalculated on every resize instead.

        Args:
            label: Label to manage.
            margin: Horizontal space taken by padding around the label.

        Returns:
            ttk.Label: The same label, for chaining onto a geometry call.
        """
        self.wrap_labels.append((label, margin))

        return label

    def _on_resize(self, event: tk.Event) -> None:
        """Recompute wrap widths after the window is resized.

        Args:
            event: The Tk configure event; only root events are acted on.
        """
        if event.widget is not self.root:
            return

        width = event.width

        if width == self.last_width:
            return

        self.last_width = width

        for label, margin in self.wrap_labels:
            label.configure(wraplength=max(280, width - margin))

    def _scrollable(self, parent: ttk.Frame) -> ttk.Frame:
        """Wrap a tab's content in a vertically scrollable area.

        The Setup tab is the tallest part of the window. Without this, a short
        screen — or a Windows display scale above 100% — pushes its buttons past
        the bottom edge where they cannot be reached.

        Args:
            parent: The tab frame.

        Returns:
            ttk.Frame: Frame to place the scrolling content in.
        """
        canvas = tk.Canvas(
            parent,
            highlightthickness=0,
            borderwidth=0,
            background=PALETTE["bg"],
        )
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=content, anchor="nw")

        def on_content_resize(_event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas_resize(event: tk.Event) -> None:
            canvas.itemconfigure(window, width=event.width)

        def on_wheel(event: tk.Event) -> None:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        content.bind("<Configure>", on_content_resize)
        canvas.bind("<Configure>", on_canvas_resize)
        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", on_wheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))

        self.setup_canvas = canvas
        self.setup_content = content

        return content

    def _center(self, width: int, height: int) -> None:
        """Size the window and centre it on screen.

        The window opens tall enough to show the Setup tab in full, so nothing
        starts out hidden, but never taller than the screen — the tab scrolls
        when it has to give way. A canvas reports no useful requested height of
        its own, so the scrolling content is measured directly.

        Args:
            width: Minimum window width in pixels.
            height: Minimum window height in pixels.
        """
        self.root.update_idletasks()

        screen_width = self.root.winfo_screenwidth()
        # Leave room for the taskbar: the screen height includes it, and a window
        # sized to the full height puts its footer underneath.
        usable_height = self.root.winfo_screenheight() - 160

        chrome = self.root.winfo_reqheight() - self.setup_canvas.winfo_reqheight()
        needed = chrome + self.setup_content.winfo_reqheight()

        width = min(max(width, self.root.winfo_reqwidth()), screen_width - 80)
        height = min(max(height, needed), usable_height)

        left = max(0, (screen_width - width) // 2)
        top = max(0, (self.root.winfo_screenheight() - height) // 3)

        self.root.minsize(min(880, width), min(560, height))
        self.root.geometry(f"{width}x{height}+{left}+{top}")

    def _build_layout(self) -> None:

        """Assemble the header, footer, tabs and activity log."""
        outer = ttk.Frame(self.root, padding=(16, 14))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))

        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        self._wrapping(
            ttk.Label(
                header,
                text=(
                    "Install Claude Desktop and register MCP servers in its "
                    "configuration file."
                ),
                style="Subtitle.TLabel",
                justify="left",
            ),
            margin=60,
        ).pack(anchor="w", pady=(2, 0))

        # This claims the bottom edge before the footer does, so it sits below
        # the footer row at the very bottom of the window.
        ttk.Label(
            outer,
            text="Part of the MSH project.",
            style="Credit.TLabel",
        ).pack(side="bottom", anchor="center")

        # The footer claims its height before the notebook does, so its buttons
        # can never be pushed off the bottom edge by tall tab content.
        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(12, 0))

        ttk.Button(footer, text="Close", command=self.on_close).pack(
            side="right", padx=(8, 0)
        )
        ttk.Button(
            footer,
            text="Re-check environment",
            command=self.refresh_environment,
        ).pack(side="right")

        self.status_label = ttk.Label(footer, text="Ready", style="Subtitle.TLabel")
        self.status_label.pack(side="left", fill="x", expand=True)

        notebook = ttk.Notebook(outer)
        notebook.pack(side="top", fill="both", expand=True)

        setup_tab = ttk.Frame(notebook, padding=(14, 14, 8, 14))
        servers_tab = ttk.Frame(notebook, padding=14)
        log_tab = ttk.Frame(notebook, padding=14)

        notebook.add(setup_tab, text="  Setup  ")
        notebook.add(servers_tab, text="  Servers  ")
        notebook.add(log_tab, text="  Activity  ")

        self._build_setup_tab(self._scrollable(setup_tab))
        self._build_servers_tab(servers_tab)
        self._build_log_tab(log_tab)

    def _build_setup_tab(self, parent: ttk.Frame) -> None:
        """Build the environment checks and the add-server form.

        Args:
            parent: Scrollable content frame of the Setup tab.
        """
        environment = self._card(
            parent,
            "1 · Environment",
            "Checked automatically. Claude, Python and the configuration file "
            "are all located at runtime, so nothing is tied to one machine.",
        )
        environment.columnconfigure(0, weight=1)

        self.claude_state = self._wrapping(
            ttk.Label(environment, text="Checking…", style="Card.TLabel", justify="left"),
            margin=110,
        )
        self.claude_state.grid(row=0, column=0, sticky="ew")

        claude_actions = ttk.Frame(environment, style="Card.TFrame")
        claude_actions.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.claude_button = ttk.Button(
            claude_actions,
            text="Download and install Claude",
            style="Accent.TButton",
            command=self.install_claude,
        )
        self.claude_button.pack(side="left")

        self.cancel_button = ttk.Button(
            claude_actions,
            text="Cancel",
            command=self.cancel_download,
            state="disabled",
        )
        self.cancel_button.pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(
            environment,
            style="Thin.Horizontal.TProgressbar",
            mode="determinate",
            maximum=100,
        )
        self.progress.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        self.progress_label = ttk.Label(environment, text="", style="Hint.TLabel")
        self.progress_label.grid(row=3, column=0, sticky="w", pady=(4, 0))

        self.python_state = self._wrapping(
            ttk.Label(environment, text="", style="Card.TLabel", justify="left"),
            margin=110,
        )
        self.python_state.grid(row=4, column=0, sticky="ew", pady=(12, 0))

        ttk.Button(
            environment,
            text="Choose interpreter…",
            command=self.browse_python,
        ).grid(row=5, column=0, sticky="w", pady=(8, 0))

        self.config_state = self._wrapping(
            ttk.Label(environment, text="", style="Card.TLabel", justify="left"),
            margin=110,
        )
        self.config_state.grid(row=6, column=0, sticky="ew", pady=(12, 0))

        self._build_server_form(parent)

    def _build_server_form(self, parent: ttk.Frame) -> None:
        """Build the form that registers a new MCP server.

        Args:
            parent: Scrollable content frame of the Setup tab.
        """
        form = self._card(
            parent,
            "2 · Add an MCP server",
            "Choose the server's entry point: a Python script (.py) or a "
            "standalone executable (.exe, .cmd, .bat). The launch command is "
            "written to match the file type.",
        )
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Name", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=4
        )

        self.name_entry = ttk.Entry(form)
        self.name_entry.grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=4
        )

        ttk.Label(
            form,
            text="Letters, digits, dot, dash and underscore.",
            style="Hint.TLabel",
        ).grid(row=1, column=1, columnspan=2, sticky="w", padx=(10, 0))

        ttk.Label(form, text="Server file", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=4
        )

        self.path_entry = ttk.Entry(form)
        self.path_entry.grid(row=2, column=1, sticky="ew", padx=(10, 8), pady=4)

        ttk.Button(form, text="Browse…", command=self.browse_server).grid(
            row=2, column=2, sticky="e", pady=4
        )

        ttk.Label(form, text="Arguments", style="Card.TLabel").grid(
            row=3, column=0, sticky="w", pady=4
        )

        self.args_entry = ttk.Entry(form)
        self.args_entry.grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=4
        )

        ttk.Label(
            form,
            text=(
                "Optional, passed after the server path. Quote values that "
                "contain spaces."
            ),
            style="Hint.TLabel",
        ).grid(row=4, column=1, columnspan=2, sticky="w", padx=(10, 0))

        ttk.Label(form, text="Command", style="Card.TLabel").grid(
            row=5, column=0, sticky="nw", pady=(10, 4)
        )

        self.preview_label = self._wrapping(
            ttk.Label(
                form,
                text="Choose a server file to see the command.",
                style="Hint.TLabel",
                justify="left",
            ),
            margin=210,
        )
        self.preview_label.grid(
            row=5, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=(10, 4)
        )

        actions = ttk.Frame(form, style="Card.TFrame")
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))

        self.save_button = ttk.Button(
            actions,
            text="Save to Claude config",
            style="Accent.TButton",
            command=self.save_server,
        )
        self.save_button.pack(side="left")

        ttk.Button(actions, text="Clear", command=self.clear_form).pack(
            side="left", padx=(8, 0)
        )

        for entry in (self.name_entry, self.path_entry, self.args_entry):
            entry.bind("<KeyRelease>", lambda _event: self.update_preview())

    def _build_servers_tab(self, parent: ttk.Frame) -> None:
        """Build the list of servers already in the configuration.

        Args:
            parent: The tab frame.
        """
        body = self._card(
            parent,
            "Configured servers",
            "Read from the live configuration file. Removing an entry rewrites "
            "the file after taking a backup.",
            expand=True,
        )
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            body,
            columns=("name", "command"),
            show="headings",
            height=9,
        )
        self.tree.heading("name", text="Name")
        self.tree.heading("command", text="Command")
        self.tree.column("name", width=180, minwidth=120, anchor="w", stretch=False)
        self.tree.column("command", width=520, minwidth=260, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")

        vertical = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vertical.set)
        vertical.grid(row=0, column=1, sticky="ns")

        horizontal = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=horizontal.set)
        horizontal.grid(row=1, column=0, sticky="ew")

        buttons = ttk.Frame(body, style="Card.TFrame")
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))

        ttk.Button(buttons, text="Reload", command=self.reload_servers).pack(side="left")
        ttk.Button(
            buttons,
            text="Remove selected",
            command=self.remove_server,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons,
            text="Open config folder",
            command=self.open_config_folder,
        ).pack(side="left", padx=(8, 0))

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        """Build the activity log view.

        Args:
            parent: The tab frame.
        """
        body = self._card(
            parent,
            "Activity log",
            "Every action this tool takes.",
            expand=True,
        )
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self.log_box = tk.Text(
            body,
            height=12,
            wrap="word",
            state="disabled",
            relief="flat",
            background="#fbfbfd",
            foreground=PALETTE["text"],
            font=("Consolas", 9) if IS_WINDOWS else ("Menlo", 10),
            padx=8,
            pady=6,
        )
        self.log_box.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.log_box.tag_configure("info", foreground=PALETTE["text"])
        self.log_box.tag_configure("ok", foreground=PALETTE["ok"])
        self.log_box.tag_configure("warn", foreground=PALETTE["warn"])
        self.log_box.tag_configure("error", foreground=PALETTE["error"])

        ttk.Button(body, text="Copy log", command=self.copy_log).grid(
            row=1, column=0, sticky="w", pady=(12, 0)
        )

    # ---------- feedback ----------

    def log(self, message: str, level: str = "info") -> None:
        """Append a timestamped line to the activity log.

        Safe to call from a worker thread: the write is marshalled onto the Tk
        thread, since Tkinter is not thread-safe.

        Args:
            message: Text to record.
            level: One of ``info``, ``ok``, ``warn`` or ``error``.
        """
        def write() -> None:
            self.log_box.configure(state="normal")
            self.log_box.insert(
                "end",
                f"[{datetime.now():%H:%M:%S}] {message}\n",
                level,
            )
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        self.call_on_ui(write)

    def set_status(self, message: str) -> None:
        """Update the footer status line.

        Args:
            message: Short description of the current activity.
        """
        self.call_on_ui(lambda: self.status_label.configure(text=message))

    def call_on_ui(self, action) -> None:
        """Run a callable on the Tk thread, ignoring a closed window.

        Detection and download work on worker threads, and the user can close
        the window while one is still running. Tk raises once the interpreter is
        gone, and there is nothing left to update at that point.

        Args:
            action: Zero-argument callable that touches widgets.
        """
        try:
            self.root.after(0, action)
        except (tk.TclError, RuntimeError):
            pass

    def copy_log(self) -> None:
        """Copy the activity log to the clipboard."""
        text = self.log_box.get("1.0", "end").strip()

        if not text:
            return

        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("Log copied to the clipboard")

    def set_busy(self, busy: bool, *, cancellable: bool = False) -> None:
        """Enable or disable the controls that start long operations.

        Args:
            busy: True while a background operation is running.
            cancellable: Whether the Cancel button should be active.
        """
        self.busy = busy
        state = "disabled" if busy else "normal"

        def apply() -> None:
            self.claude_button.configure(state=state)
            self.save_button.configure(state=state)
            self.cancel_button.configure(
                state="normal" if busy and cancellable else "disabled"
            )

        self.call_on_ui(apply)

    # ---------- environment ----------

    def refresh_environment(self) -> None:
        """Re-detect Claude, Python and the config file on a worker thread."""
        if self.busy:
            return

        self.set_status("Checking the environment…")
        self.claude_state.configure(text="Checking…", style="Card.TLabel")
        self.python_state.configure(text="Checking…", style="Card.TLabel")

        threading.Thread(target=self._detect_worker, daemon=True).start()

    def _detect_worker(self) -> None:
        """Probe the machine and push the results back to the UI."""
        claude = find_claude() if IS_WINDOWS else None
        python = find_python()
        running = claude_is_running() if claude else False

        self.claude_path = claude
        self.python_path = python
        self.config_file = config_path()

        if claude is not None:
            version = claude_version(claude)
            suffix = f" (v{version})" if version else ""
            self.call_on_ui(
                lambda: self.claude_state.configure(
                    text=f"Claude Desktop is installed{suffix}\n{claude}",
                    style="Ok.TLabel",
                )
            )
            self.call_on_ui(
                lambda: self.claude_button.configure(text="Reinstall / update Claude")
            )
            self.log(f"Claude Desktop found: {claude}", "ok")

            if running:
                self.log(
                    "Claude Desktop is running. Restart it after saving so the "
                    "new server is picked up.",
                    "warn",
                )
        else:
            message = (
                "Claude Desktop was not found"
                if IS_WINDOWS
                else "Claude Desktop detection requires Windows"
            )
            self.call_on_ui(
                lambda: self.claude_state.configure(text=message, style="Warn.TLabel")
            )
            self.call_on_ui(
                lambda: self.claude_button.configure(
                    text="Download and install Claude",
                    state="normal" if IS_WINDOWS else "disabled",
                )
            )
            self.log("Claude Desktop is not installed on this machine.", "warn")

        if python is not None:
            label = python_label(python)
            self.call_on_ui(
                lambda: self.python_state.configure(
                    text=f"Python: {label}", style="Ok.TLabel"
                )
            )
            self.log(f"Python interpreter: {label}", "ok")
        else:
            self.call_on_ui(
                lambda: self.python_state.configure(
                    text=(
                        "Python: not found. Install Python 3.10+ or choose an "
                        "interpreter manually to configure .py servers."
                    ),
                    style="Warn.TLabel",
                )
            )
            self.log("No Python interpreter was found.", "warn")

        self.call_on_ui(self._refresh_config_state)
        self.call_on_ui(self.reload_servers)
        self.call_on_ui(self.update_preview)
        self.set_status("Ready")

    def _refresh_config_state(self) -> None:
        """Describe the configuration file's current state."""
        path = self.config_file
        packaged = "LocalCache" in path.parts
        kind = "packaged install" if packaged else "standard install"

        if path.is_file():
            try:
                count = len(list_servers(load_config(path)))
            except ConfigError as error:
                self.config_state.configure(
                    text=f"Config: {error}", style="Error.TLabel"
                )
                self.log(str(error), "error")
                return

            plural = "" if count == 1 else "s"
            self.config_state.configure(
                text=(
                    f"Config ({kind}): {path}\n{count} server{plural} configured"
                ),
                style="Ok.TLabel",
            )
        else:
            self.config_state.configure(
                text=(
                    f"Config ({kind}): {path}\nNot created yet; it will be "
                    f"written on save."
                ),
                style="Card.TLabel",
            )


    def browse_python(self) -> None:
        """Let the user pick the interpreter used for Python servers."""
        initial = str(self.python_path.parent) if self.python_path else str(Path.home())
        selected = filedialog.askopenfilename(
            title="Select a Python interpreter",
            initialdir=initial,
            filetypes=INTERPRETER_FILETYPES,
        )

        if not selected:
            return

        candidate = console_interpreter(Path(selected))
        probe = run_hidden([str(candidate), "--version"])

        if "python" not in probe.lower():
            messagebox.showerror(
                APP_NAME,
                f"{candidate.name} did not respond to --version as a Python "
                f"interpreter, so it was not selected.",
            )
            self.log(f"Rejected interpreter: {candidate}", "error")
            return

        self.python_path = candidate
        label = python_label(candidate)
        self.python_state.configure(text=f"Python: {label}", style="Ok.TLabel")
        self.log(f"Interpreter set manually: {label}", "ok")
        self.update_preview()

    # ---------- installation ----------

    def install_claude(self) -> None:
        """Confirm, then download and launch the Claude Desktop installer."""
        if self.busy or not IS_WINDOWS:
            return

        already = self.claude_path is not None
        question = (
            "Claude Desktop is already installed. Download the latest installer "
            "and run it again?"
            if already
            else "Download the Claude Desktop installer and run it now?"
        )

        if not messagebox.askyesno(APP_NAME, question):
            self.log("Installation cancelled by the user.", "info")
            return

        self.cancel_flag.clear()
        self.set_busy(True, cancellable=True)
        threading.Thread(target=self._install_worker, daemon=True).start()

    def cancel_download(self) -> None:
        """Ask the running download to stop at the next chunk."""
        if self.busy:
            self.cancel_flag.set()
            self.set_status("Cancelling…")

    def _install_worker(self) -> None:
        """Download the architecture-appropriate installer and start it."""
        arch = machine_arch()
        destination = download_dir() / f"Claude-Setup-{arch}.exe"

        self.log(f"Architecture detected: {arch}", "info")
        self.set_status("Downloading the Claude Desktop installer…")

        installer = None
        reason = ""

        for index, url in enumerate(installer_urls(arch), 1):
            if self.cancel_flag.is_set():
                break

            if index > 1:
                self.log(f"Retrying with an alternative link: {url}", "info")

            try:
                installer = download_installer(
                    url,
                    destination,
                    self._on_download_progress,
                    self.cancel_flag.is_set,
                )
                break
            except DownloadCancelled:
                self.log("Download cancelled.", "warn")
                self._reset_progress()
                self.set_busy(False)
                self.set_status("Download cancelled")
                return
            except urllib.error.HTTPError as error:
                reason = f"HTTP {error.code} {error.reason} from {url}"
                self.log(f"Download failed: {reason}", "warn")
            except (urllib.error.URLError, OSError, ValueError) as error:
                reason = f"{error} ({url})"
                self.log(f"Download failed: {reason}", "warn")

        if installer is None:
            # `reason` is carried in a local because Python unbinds the
            # `except ... as` name once the block exits, and the dialog runs
            # later on the UI thread.
            message = reason or "the download did not complete"
            self._reset_progress()
            self.set_busy(False)
            self.set_status("Download failed")
            self.log(f"Could not download the installer: {message}", "error")
            self.call_on_ui(lambda: self._offer_manual_download(message))
            return

        size = installer.stat().st_size
        self.log(
            f"Installer saved to {installer} ({human_size(size)}).",
            "ok",
        )
        self.set_status("Starting the installer…")

        try:
            launch_installer(installer)
        except OSError as error:
            reason = str(error)
            self.set_busy(False)
            self.log(f"The installer could not be started: {reason}", "error")
            self.call_on_ui(
                lambda: messagebox.showerror(
                    APP_NAME,
                    f"The installer was downloaded to\n{installer}\n\nbut could "
                    f"not be started automatically: {reason}\n\nRun it manually, "
                    f"then use 'Re-check environment'.",
                )
            )
            return

        self.log(
            "The installer is running. Finish it, then this tool will re-check "
            "automatically.",
            "info",
        )
        self.set_busy(False)
        self._wait_for_install()

    def _on_download_progress(self, downloaded: int, total: int) -> None:
        """Update the progress bar and its caption.

        Args:
            downloaded: Bytes received so far.
            total: Expected total, or 0 when the server sent no length.
        """
        def apply() -> None:
            if total > 0:
                percent = downloaded * 100 / total
                self.progress.configure(mode="determinate", value=percent)
                self.progress_label.configure(
                    text=(
                        f"{human_size(downloaded)} of {human_size(total)} "
                        f"({percent:.0f}%)"
                    )
                )
            else:
                # No Content-Length, so there is no percentage to show; the
                # byte counter is the only honest indicator.
                self.progress.configure(mode="determinate", value=0)
                self.progress_label.configure(
                    text=f"{human_size(downloaded)} downloaded"
                )

        self.call_on_ui(apply)

    def _reset_progress(self) -> None:
        """Clear the progress bar and its caption."""
        def apply() -> None:
            self.progress.configure(mode="determinate", value=0)
            self.progress_label.configure(text="")

        self.call_on_ui(apply)

    def _offer_manual_download(self, reason: str) -> None:
        """Fall back to the download page when every link fails.

        Args:
            reason: Error text shown to the user.
        """
        open_page = messagebox.askyesno(
            APP_NAME,
            f"The installer could not be downloaded:\n\n{reason}\n\n"
            f"Open the Claude Desktop download page in your browser so you can "
            f"download it manually?",
        )

        if open_page:
            webbrowser.open(CLAUDE_DOWNLOAD_PAGE)
            self.log(f"Opened {CLAUDE_DOWNLOAD_PAGE} in the browser.", "info")
            self.log(
                "After installing, use 'Re-check environment' to pick it up.",
                "info",
            )


    def _wait_for_install(self) -> None:
        """Poll for a completed installation for a few minutes.

        The installer runs in its own process, so completion is detected by
        watching for the executable to appear rather than by waiting on an exit
        code. The polling runs on a worker thread because detection shells out
        and would otherwise freeze the window between checks.
        """
        def poll() -> None:
            deadline = time.monotonic() + 300

            while time.monotonic() < deadline:
                if self.cancel_flag.is_set():
                    return

                if find_claude() is not None:
                    self.log("Claude Desktop installation detected.", "ok")
                    self._reset_progress()
                    self.call_on_ui(self.refresh_environment)
                    return

                time.sleep(3)

            self.log(
                "Still no Claude Desktop installation detected. Use "
                "'Re-check environment' once the installer finishes.",
                "warn",
            )
            self._reset_progress()

        self.set_status("Waiting for the installation to finish…")
        threading.Thread(target=poll, daemon=True).start()


    # ---------- add-server form ----------

    def browse_server(self) -> None:
        """Pick the MCP server entry point and suggest a name for it."""
        selected = filedialog.askopenfilename(
            title="Select the MCP server entry point",
            filetypes=SERVER_FILETYPES,
        )

        if not selected:
            return

        target = Path(selected)
        self.path_entry.delete(0, "end")
        self.path_entry.insert(0, str(target))

        if not self.name_entry.get().strip():
            self.name_entry.delete(0, "end")
            self.name_entry.insert(0, self.suggest_name(target))

        self.log(f"Selected server file: {target}", "info")
        self.update_preview()

    def suggest_name(self, target: Path) -> str:
        """Derive a valid server name from a file path.

        ``main.py`` is a common entry-point name that says nothing about the
        server, so the parent directory is used in that case.

        Args:
            target: Selected server file.

        Returns:
            str: Suggested name, always matching ``NAME_PATTERN``.
        """
        stem = target.stem

        if stem.lower() in ("main", "server", "app", "__main__", "index"):
            stem = target.parent.name or stem

        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._").lower()

        return cleaned or "mcp-server"

    def form_values(self) -> tuple[str, str, str]:
        """Read the trimmed contents of the add-server form.

        Returns:
            tuple[str, str, str]: Name, server path and extra arguments.
        """
        return (
            self.name_entry.get().strip(),
            self.path_entry.get().strip().strip('"'),
            self.args_entry.get().strip(),
        )

    def update_preview(self) -> None:
        """Show the command that will be written, or why it cannot be built."""
        _, raw_path, extra = self.form_values()

        if not raw_path:
            self.preview_label.configure(
                text="Choose a server file to see the command.",
                style="Hint.TLabel",
            )
            return

        target = Path(os.path.expandvars(raw_path)).expanduser()

        if not target.is_file():
            self.preview_label.configure(
                text="That path does not point to an existing file.",
                style="Error.TLabel",
            )
            return

        try:
            entry = build_entry(target, self.python_path, extra)
        except ConfigError as error:
            self.preview_label.configure(text=str(error), style="Error.TLabel")
            return

        suffix = target.suffix.lower()

        if suffix in (".py", ".pyw"):
            kind = "Python script, launched with the interpreter above"
        elif suffix in (".js", ".mjs", ".cjs"):
            kind = "Node script, launched with node.exe"
        else:
            kind = "Executable, launched directly"

        self.preview_label.configure(
            text=f"{describe_entry(entry)}\n{kind}",
            style="Hint.TLabel",
        )


    def clear_form(self) -> None:
        """Empty the add-server form."""
        for entry in (self.name_entry, self.path_entry, self.args_entry):
            entry.delete(0, "end")

        self.update_preview()

    def validate_form(self) -> tuple[str, Path, str] | None:
        """Validate the form and report the first problem found.

        Returns:
            tuple[str, Path, str] | None: Name, resolved path and extra
            arguments, or None when the form is not usable.
        """
        name, raw_path, extra = self.form_values()

        if not name:
            messagebox.showwarning(APP_NAME, "Enter a name for the server.")
            self.name_entry.focus_set()
            return None

        if not NAME_PATTERN.match(name):
            messagebox.showwarning(
                APP_NAME,
                "The server name must start with a letter or digit and may "
                "contain only letters, digits, dots, dashes and underscores.",
            )
            self.name_entry.focus_set()
            return None

        if not raw_path:
            messagebox.showwarning(APP_NAME, "Choose the server's entry point.")
            self.path_entry.focus_set()
            return None

        target = Path(os.path.expandvars(raw_path)).expanduser()

        try:
            target = target.resolve(strict=True)
        except OSError:
            messagebox.showerror(
                APP_NAME,
                f"This file does not exist:\n{raw_path}",
            )
            self.path_entry.focus_set()
            return None

        if not target.is_file():
            messagebox.showerror(
                APP_NAME,
                f"This is not a file:\n{target}",
            )
            self.path_entry.focus_set()
            return None

        return name, target, extra

    # ---------- writing the configuration ----------

    def save_server(self) -> None:
        """Validate the form and write the server into the configuration."""
        if self.busy:
            return

        validated = self.validate_form()

        if validated is None:
            return

        name, target, extra = validated

        try:
            entry = build_entry(target, self.python_path, extra)
            config = load_config(self.config_file)
        except ConfigError as error:
            messagebox.showerror(APP_NAME, str(error))
            self.log(str(error), "error")
            return

        servers = list_servers(config)

        if name in servers:
            existing = describe_entry(servers[name])
            replace = messagebox.askyesno(
                APP_NAME,
                f"A server named '{name}' already exists:\n\n{existing}\n\n"
                f"Replace it with:\n\n{describe_entry(entry)}",
            )

            if not replace:
                self.log(f"Kept the existing '{name}' entry.", "info")
                return

        try:
            backup = backup_config(self.config_file)
            config["mcpServers"] = {**servers, name: entry}
            save_config(self.config_file, config)
        except ConfigError as error:
            messagebox.showerror(APP_NAME, str(error))
            self.log(str(error), "error")
            return

        if backup is not None:
            self.log(f"Backup written to {backup.name}.", "info")

        self.log(f"Saved '{name}': {describe_entry(entry)}", "ok")
        self.log(f"Configuration file: {self.config_file}", "info")
        self.set_status(f"'{name}' saved")
        self._refresh_config_state()
        self.reload_servers()

        note = (
            "\n\nClaude Desktop is running — quit it completely (right-click "
            "its system tray icon and choose Quit) and reopen it to load the "
            "new server."
            if claude_is_running()
            else "\n\nStart Claude Desktop to use it."
        )
        messagebox.showinfo(
            APP_NAME,
            f"'{name}' was added to:\n{self.config_file}{note}",
        )

    def reload_servers(self) -> None:
        """Refill the servers table from the configuration file."""
        for item in self.tree.get_children():
            self.tree.delete(item)

        try:
            servers = list_servers(load_config(self.config_file))
        except ConfigError as error:
            self.tree.insert("", "end", values=("(error)", str(error)))
            return

        if not servers:
            self.tree.insert("", "end", values=("(none)", "No MCP servers configured"))
            return

        for name in sorted(servers):
            self.tree.insert(
                "",
                "end",
                iid=name,
                values=(name, describe_entry(servers[name])),
            )


    def remove_server(self) -> None:
        """Delete the selected server from the configuration."""
        selection = self.tree.selection()

        if not selection:
            messagebox.showinfo(APP_NAME, "Select a server to remove.")
            return

        name = selection[0]

        try:
            config = load_config(self.config_file)
        except ConfigError as error:
            messagebox.showerror(APP_NAME, str(error))
            self.log(str(error), "error")
            return

        servers = list_servers(config)

        if name not in servers:
            self.reload_servers()
            return

        confirm = messagebox.askyesno(
            APP_NAME,
            f"Remove '{name}' from the Claude configuration?\n\n"
            f"{describe_entry(servers[name])}\n\n"
            f"A backup of the config file is written first. The server's own "
            f"files are not touched.",
        )

        if not confirm:
            return

        try:
            backup = backup_config(self.config_file)
            remaining = {key: value for key, value in servers.items() if key != name}
            config["mcpServers"] = remaining
            save_config(self.config_file, config)
        except ConfigError as error:
            messagebox.showerror(APP_NAME, str(error))
            self.log(str(error), "error")
            return

        if backup is not None:
            self.log(f"Backup written to {backup.name}.", "info")

        self.log(f"Removed '{name}' from the configuration.", "ok")
        self.set_status(f"'{name}' removed")
        self._refresh_config_state()
        self.reload_servers()

    def open_config_folder(self) -> None:
        """Reveal the configuration directory in the file manager."""
        folder = self.config_file.parent

        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            messagebox.showerror(APP_NAME, f"Could not open {folder}: {error}")
            return

        try:
            if IS_WINDOWS:
                os.startfile(str(folder))  # noqa: S606 - user-initiated
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as error:
            messagebox.showerror(APP_NAME, f"Could not open {folder}: {error}")
            return

        self.log(f"Opened {folder}.", "info")

    # ---------- shutdown ----------

    def on_close(self) -> None:
        """Confirm before closing while a download is in progress."""
        if self.busy:
            if not messagebox.askyesno(
                APP_NAME,
                "An operation is still running. Cancel it and close?",
            ):
                return

            self.cancel_flag.set()

        self.root.destroy()


# ============================================================
# Entry point
# ============================================================

def main() -> int:
    """Run the setup window.

    Returns:
        int: Process exit code; 1 when no display was available.
    """
    try:
        root = tk.Tk()
    except tk.TclError as error:
        print(f"{APP_NAME}: no graphical display available ({error})", file=sys.stderr)
        return 1

    SetupWindow(root)
    root.mainloop()

    return 0


if __name__ == "__main__":
    sys.exit(main())

