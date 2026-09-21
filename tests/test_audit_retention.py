#!/usr/bin/env python3
"""D6 audit retention policy: configured, shared, and at least 400 days."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import casekit  # noqa: E402
from core.config.overlay import (  # noqa: E402
    ConfigOverlayError,
    load_overlay_dir,
    validate_audit_retention_policy,
)


def d6_valid_minimum() -> bool:
    base = {"shared": {"audit_retention_days": 400}}
    return validate_audit_retention_policy(base) == 400


def d6_real_config_is_compliant() -> bool:
    data = load_overlay_dir(ROOT / "infra" / "config")
    return validate_audit_retention_policy(data["base"]) == 400


def d6_missing_policy_rejected() -> None:
    validate_audit_retention_policy({"shared": {}})


def d6_short_policy_rejected() -> None:
    validate_audit_retention_policy({"shared": {"audit_retention_days": 399}})


def d6_bool_policy_rejected() -> None:
    validate_audit_retention_policy({"shared": {"audit_retention_days": True}})


def d6_string_policy_rejected() -> None:
    validate_audit_retention_policy({"shared": {"audit_retention_days": "400"}})


CASES = [
    ("D6-1 minimum 400 days is accepted", d6_valid_minimum),
    ("D6-2 repository base config is compliant", d6_real_config_is_compliant),
]
REJECT_CASES = [
    ("D6-3 missing retention policy is rejected", ConfigOverlayError,
     d6_missing_policy_rejected),
    ("D6-4 retention below 400 is rejected", ConfigOverlayError,
     d6_short_policy_rejected),
    ("D6-5 bool retention is rejected", ConfigOverlayError,
     d6_bool_policy_rejected),
    ("D6-6 string retention is rejected", ConfigOverlayError,
     d6_string_policy_rejected),
]
FAILMSG_CASES = [
    ("D6-7 rejection identifies the 400-day floor",
     d6_short_policy_rejected, "400"),
]

test_pass, test_reject, test_failmsg = casekit.pytest_cases(
    CASES, REJECT_CASES, FAILMSG_CASES)


if __name__ == "__main__":
    raise SystemExit(casekit.run_cli(
        "D6 audit retention policy", CASES, REJECT_CASES, FAILMSG_CASES))
