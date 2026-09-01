"""Install the MSHCore package for the ModelSetupHub MCP server.

The server imports MSHCore as an installed package, so it has to be present
before ``main.py`` can start. This script installs it from the ``Core``
submodule with pip::

    python utils/install_mshcore.py            # regular install
    python utils/install_mshcore.py --editable # editable (development) install
    python utils/install_mshcore.py --upgrade  # reinstall with --force-reinstall

The script locates the submodule relative to its own file, so it runs from any
working directory. Only the standard library is used, and pip is invoked in-process
(``runpy``) rather than as a subprocess, so the same interpreter that will run
the server receives the package.

MSHCore needs ``psutil`` at runtime; the submodule's readme covers it, and the
server's ``requirements.txt`` lists it.
"""

from __future__ import annotations

import argparse
import importlib.util
import runpy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = PROJECT_ROOT / "Core"
PYPROJECT = CORE_ROOT / "pyproject.toml"


def core_submodule_present() -> bool:
    """Return whether the Core submodule is checked out and pip-installable."""
    return (CORE_ROOT / "MSHCore" / "__init__.py").is_file() and PYPROJECT.is_file()


def mshcore_installed() -> bool:
    """Return whether the MSHCore package is importable in this interpreter."""
    return importlib.util.find_spec("MSHCore") is not None


def install_mshcore(editable: bool = False, force: bool = False) -> int:
    """Install MSHCore from the Core submodule using pip.

    Args:
        editable: Install in editable mode (``pip install -e``).
        force: Pass ``--force-reinstall``, refreshing an existing install.

    Returns:
        int: The pip exit code, 0 on success.
    """
    if not core_submodule_present():
        print(
            f"The Core submodule was not found at {CORE_ROOT}. Check it out "
            f"first: run 'git submodule update --init' in {PROJECT_ROOT}."
        )
        return 1

    target = str(CORE_ROOT)
    args = ["-m", "pip", "install"]
    if editable:
        args.append("-e")
    if force:
        args.append("--force-reinstall")
    args.append(target)

    print(f"Installing MSHCore from {target} ...")
    print(f"    pip install {' '.join(args[3:])}")

    # pip's API is not public, so run it as __main__ inside this interpreter —
    # equivalent to 'python -m pip install ...' on the command line, without
    # needing a subprocess.
    sys.argv = ["pip", "install", *args[3:]]
    try:
        runpy.run_module("pip", run_name="__main__")
    except SystemExit as exit_code:
        code = exit_code.code
        return code if isinstance(code, int) else (0 if code is None else 1)

    print("\nMSHCore installed. Verify with:")
    print(f"    {sys.executable} -c \"import MSHCore; print(MSHCore.__file__)\"")
    return 0


def main() -> int:
    """Parse arguments and run the installation."""
    parser = argparse.ArgumentParser(
        description="Install the MSHCore package from the Core submodule."
    )
    parser.add_argument(
        "-e",
        "--editable",
        action="store_true",
        help="install in editable mode (development install)",
    )
    parser.add_argument(
        "--force",
        "--upgrade",
        dest="force",
        action="store_true",
        help="force-reinstall even if MSHCore is already installed",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report whether MSHCore is installed, changing nothing",
    )
    options = parser.parse_args()

    if options.check:
        installed = mshcore_installed()
        state = "installed" if installed else "NOT installed"
        print(f"MSHCore is {state} for {sys.executable}.")
        if not installed:
            print("Run this script without --check to install it.")
        return 0

    if mshcore_installed() and not options.force:
        print("MSHCore is already installed. Use --force to reinstall it.")
        return 0

    return install_mshcore(editable=options.editable, force=options.force)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInstallation cancelled.")
        raise SystemExit(130) from None
