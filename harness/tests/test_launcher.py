"""End-to-end checks that the launcher scripts (super-agent / super-agent.bat)
forward argv to `python -m harness.cli` unchanged, so --version reaches the
real argparse action="version" implementation instead of being special-cased
in the launcher itself."""
import platform
import subprocess
from pathlib import Path

from harness._version import __version__

REPO = Path(__file__).resolve().parents[2]


def _launcher_argv():
    """Build the OS-appropriate subprocess argv for invoking the launcher.

    .bat files are not directly executable via CreateProcess without a shell,
    so on Windows we go through `cmd /c`; on POSIX the script's own shebang
    is executable directly.
    """
    if platform.system() == "Windows":
        return ["cmd", "/c", str(REPO / "super-agent.bat"), "--version"]
    return [str(REPO / "super-agent"), "--version"]


def test_launcher_script_forwards_version_flag():
    result = subprocess.run(
        _launcher_argv(),
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == f"super-agent {__version__}"
