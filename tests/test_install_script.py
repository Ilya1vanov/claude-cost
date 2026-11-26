"""install.sh tells a used-up GitHub API limit apart from a network failure (#145).

A fake `curl` on PATH plays the GitHub API; every case stops before the install step.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"

FAKE_CURL = """#!/bin/sh
# Honors -o, -D and -w like curl; FAKE_* env vars pick the answer.
out=/dev/null; headers=/dev/null; fmt=
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift ;;
    -D) headers=$2; shift ;;
    -w) fmt=$2; shift ;;
  esac
  shift
done
[ "${FAKE_EXIT:-0}" = 0 ] || exit "$FAKE_EXIT"
printf 'HTTP/2 %s\\r\\n%s\\r\\n' "$FAKE_STATUS" "${FAKE_HEADER:-}" > "$headers"
printf '%s' "${FAKE_BODY:-}" > "$out"
[ -z "$fmt" ] || printf '%s' "$FAKE_STATUS"
"""


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("uv", "#!/bin/sh\nexit 0\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)

    def _run(**fake):
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
               **{f"FAKE_{k.upper()}": str(v) for k, v in fake.items()}}
        done = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
        assert done.returncode == 1, done.stdout + done.stderr
        return done.stderr

    return _run


def test_network_failure_says_check_connection(run):
    assert "Check your connection" in run(exit=6)


def limit_headers(seconds_to_reset):
    return f"x-ratelimit-remaining: 0\nx-ratelimit-reset: {int(time.time()) + seconds_to_reset}"


@pytest.mark.parametrize("seconds, wait", [(5 * 60, "5 minutes"), (30, "1 minute"),
                                           (-60, "a few minutes")])
def test_403_with_limit_used_up_says_minutes_until_reset(run, seconds, wait):
    err = run(status=403, header=limit_headers(seconds))
    assert "limit" in err and f"Try again in {wait}." in err
    assert "connection" not in err


def test_403_with_retry_after_is_a_limit(run):
    err = run(status=403, header="retry-after: 120")
    assert "limit" in err and "Try again in 2 minutes." in err


def test_403_without_limit_headers_names_the_status(run):
    """A proxy or firewall that blocks api.github.com answers 403 too."""
    err = run(status=403)
    assert "HTTP 403" in err and "limit" not in err


def test_429_without_headers_says_a_few_minutes(run):
    err = run(status=429)
    assert "limit" in err and "Try again in a few minutes." in err


def test_429_prefers_retry_after(run):
    err = run(status=429, header="retry-after: 3600\nx-ratelimit-reset: 1")
    assert "Try again in 60 minutes." in err


def test_200_goes_on_to_install_the_wheel(run):
    body = '{"assets": [{"browser_download_url": "https://example.com/claude_cost-1.0-py3-none-any.whl"}]}'
    err = run(status=200, body=body)  # the stub uv installs nothing, so step 4 fails
    assert "claude-cost command was not found" in err


def test_other_http_error_names_the_status(run):
    err = run(status=404)
    assert "HTTP 404" in err
    assert "Try again" not in err and "no installable file" not in err
