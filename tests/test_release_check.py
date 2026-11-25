import pytest

from claude_cost.release_check import ReleaseCheckError, check_release

CHANGELOG = """# Changelog

## v0.2.0 - 2026-01-02

You can now update claude-cost with one command.

## v0.1.0 - 2026-01-01

First release.
"""


def test_returns_the_changelog_entry_for_the_tag():
    entry = check_release("v0.2.0", "0.2.0", CHANGELOG)
    assert entry == "You can now update claude-cost with one command."


def test_last_entry_is_found():
    assert check_release("v0.1.0", "0.1.0", CHANGELOG) == "First release."


def test_fails_when_tag_and_pyproject_version_differ():
    with pytest.raises(ReleaseCheckError, match="0.1.0"):
        check_release("v0.2.0", "0.1.0", CHANGELOG)


def test_fails_when_tag_has_no_v_prefix():
    with pytest.raises(ReleaseCheckError):
        check_release("0.2.0", "0.2.0", CHANGELOG)


def test_fails_when_changelog_has_no_entry():
    with pytest.raises(ReleaseCheckError, match="v0.3.0"):
        check_release("v0.3.0", "0.3.0", CHANGELOG)


def test_fails_when_entry_is_empty():
    with pytest.raises(ReleaseCheckError):
        check_release("v0.2.0", "0.2.0", "## v0.2.0 - 2026-01-02\n\n## v0.1.0\n\nx\n")
