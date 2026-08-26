#!/usr/bin/env python3
"""Prepare the T0-T6 executor for a clean GitHub Actions runtime."""

from __future__ import annotations

import argparse
from pathlib import Path


INVALID_JOB_ENV = '      HERMES_HOME: $' + '{{ runner.temp }}/hermes-home\n'
FORK_JOB = "  fork-python-regressions:\n"
CHECKOUT_SEQUENCE = (
    "      - uses: actions/checkout@v4\n"
    "        with:\n"
    "          fetch-depth: 0\n"
    "      - uses: actions/setup-python@v5\n"
)
CHECKOUT_REPLACEMENT = (
    "      - uses: actions/checkout@v4\n"
    "        with:\n"
    "          fetch-depth: 0\n"
    "      - name: Initialize isolated Hermes home\n"
    "        run: echo \"HERMES_HOME=${RUNNER_TEMP}/hermes-home\" >> \"$GITHUB_ENV\"\n"
    "      - uses: actions/setup-python@v5\n"
)


def prepare(source: Path, destination: Path) -> None:
    text = source.read_text(encoding="utf-8")
    if text.count(INVALID_JOB_ENV) != 1:
        raise RuntimeError(
            "expected exactly one runner.temp job-level environment entry; "
            f"found {text.count(INVALID_JOB_ENV)}"
        )

    patched = text.replace(INVALID_JOB_ENV, "", 1)
    job_start = patched.find(FORK_JOB)
    if job_start < 0:
        raise RuntimeError("fork-python-regressions job not found")
    checkout_index = patched.find(CHECKOUT_SEQUENCE, job_start)
    if checkout_index < 0:
        raise RuntimeError("fork-python-regressions checkout sequence not found")
    patched = (
        patched[:checkout_index]
        + CHECKOUT_REPLACEMENT
        + patched[checkout_index + len(CHECKOUT_SEQUENCE):]
    )

    legacy_helper = "test_ownership"
    helper_occurrences = patched.count(legacy_helper)
    if helper_occurrences < 4:
        raise RuntimeError(
            "ownership helper rename surface is incomplete: "
            f"{helper_occurrences} occurrences"
        )
    patched = patched.replace(legacy_helper, "ownership")

    if INVALID_JOB_ENV in patched:
        raise RuntimeError("runner.temp job-level environment entry remains")
    if legacy_helper in patched:
        raise RuntimeError("legacy ownership helper name remains")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(patched, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.destination)


if __name__ == "__main__":
    main()
