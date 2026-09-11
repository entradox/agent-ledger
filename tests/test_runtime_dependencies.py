# tests/test_runtime_dependencies.py
"""Guards a failure class the rest of this suite structurally CANNOT catch.

On 2026-09-11 the workspace-identity deploy shipped with `x402[evm]>=2.0` but
without `httpx`. Everything passed locally -- 183/183, plus an end-to-end probe
against a production-shaped data dir -- because a dev machine almost always has
httpx already, as a transitive dependency of something else. In the clean
Railway image httpx was absent, so x402's HTTP facilitator client could not be
constructed, X402_ENABLED stayed False, and the self-serve mint route returned
503 instead of 402 to every caller.

No test that runs in this repo can detect that, because the dependency IS
importable in the environment the tests run in. The only thing that can catch it
is asserting the DECLARATION, which is what this file does.

When adding a runtime dependency that a library hides behind an extra other than
the one you installed, add it to requirements.txt AND extend the list below.
"""
import re
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"

# Packages that are load-bearing at runtime but arrive via a non-obvious route.
#   httpx: x402 declares it under [clients]/[all], not [evm].
REQUIRED_EXPLICITLY = {
    "httpx": "x402's HTTP facilitator client needs it; x402 declares it only "
             "under the [clients]/[all] extras, not [evm]",
    "x402": "the x402 payment rail itself",
}


def _declared() -> dict:
    """Map requirement name -> raw line, ignoring comments and blanks."""
    out = {}
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!\[; ]", line, maxsplit=1)[0].strip().lower()
        if name:
            out[name] = line
    return out


def test_requirements_file_exists():
    assert REQUIREMENTS.exists(), f"missing {REQUIREMENTS}"


def test_runtime_critical_packages_are_declared():
    declared = _declared()
    missing = [f"{n} ({why})" for n, why in REQUIRED_EXPLICITLY.items()
               if n not in declared]
    assert not missing, (
        "requirements.txt does not declare a runtime dependency that a clean "
        "image needs -- the local suite cannot catch this, production will "
        "degrade silently instead:\n  " + "\n  ".join(missing))


def test_x402_evm_extra_is_the_one_in_use():
    """Documents the pairing that broke: the x402 extra and httpx are a set.

    If someone changes the extra (e.g. to x402[all], which DOES pull httpx),
    this test tells them they may no longer need the explicit line -- rather
    than leaving a stale comment behind.
    """
    declared = _declared()
    assert "x402" in declared, "the x402 requirement disappeared from requirements.txt"
    assert "[evm]" in declared["x402"], (
        "the x402 extra changed away from [evm]; re-check whether httpx still "
        "needs its own line -- x402[all] and x402[clients] both pull it in"
    )
