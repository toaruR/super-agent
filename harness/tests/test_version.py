import re

from harness._version import __version__

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def test_version_is_string_type():
    assert isinstance(__version__, str)


def test_version_initial_value_is_0_1_0():
    assert __version__ == "0.1.0"


def test_version_matches_semver_format():
    assert SEMVER_RE.match(__version__)
