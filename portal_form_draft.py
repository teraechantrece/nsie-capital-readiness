"""Controlled browser form-filling proof of concept for NSIE.

External portal form filling can transmit data via JavaScript/autosave *even
without clicking Submit*. This script therefore requires an explicit,
portal-specific disclosure approval. It never clicks, uploads, certifies,
authorizes a credit check, or submits applications.

Uses playwright (optional dependency); no persistent browser profile.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PRIVATE_FIELDS = {
    "ein", "tax_id", "ssn", "social_security_number", "dob", "date_of_birth",
    "bank_account", "account_number", "routing_number", "password", "credit_score",
    "owner_home_address", "owner_phone", "driver_license",
}
DANGEROUS_LABEL = re.compile(r"(?i)(submit|certif\w*|authoriz\w*|credit check|hard inquiry|agree|consent|signature|guarantee|pay (?:a |the )?fee)")


def validate_portal(spec: dict[str, Any]) -> str:
    address = str(spec.get("url", ""))
    parts = urlsplit(address)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Portal must be an HTTPS URL with no embedded credentials")
    hosts = spec.get("allowed_hosts")
    if not isinstance(hosts, list) or not hosts or any(not isinstance(x, str) for x in hosts):
        raise ValueError("Portal host allowlist required")
    if parts.hostname.lower() not in {host.lower() for host in hosts}:
        raise ValueError("Portal host is not in approved allowlist")
    if not isinstance(spec.get("mappings"), list) or not spec["mappings"]:
        raise ValueError("At least one label-based mapping required")
    return parts.hostname.lower()


def validated_fields(case: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, str]]:
    validate_portal(spec)
    result: list[dict[str, str]] = []
    used = set()
    fields = case.get("fields") or {}
    for entry in spec["mappings"]:
        if not isinstance(entry, dict):
            raise ValueError("Invalid field mapping")
        field = entry.get("field")
        label = entry.get("label")
        if not isinstance(field, str) or not isinstance(label, str) or not label.strip():
            raise ValueError("Field and label must be named")
        if field in PRIVATE_FIELDS or DANGEROUS_LABEL.search(label):
            raise ValueError("Protected or legally consequential field cannot be autofilled by this worker")
        if field in used:
            raise ValueError("Duplicate field mapping")
        used.add(field)
        evidence = fields.get(field)
        if not isinstance(evidence, dict):
            raise ValueError(f"Missing evidence for field: {field}")
        if evidence.get("evidence_status") != "verified" or not evidence.get("source_ref"):
            raise ValueError(f"Unverified evidence for field: {field}")
        value = evidence.get("value")
        if not isinstance(value, (str, int, float)) or not str(value).strip():
            raise ValueError(f"Empty or unsupported value for field: {field}")
        result.append({"field": field, "label": label, "value": str(value)})
    return result


async def fill_portal(spec: dict[str, Any], fields: list[dict[str, str]]) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"state": "BLOCKED", "reason": "Install playwright and Chromium in the authorized runtime"}
    host = validate_portal(spec)
    async with async_playwright() as browser_engine:
        browser = await browser_engine.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=False)
        page = await context.new_page()
        completed: list[str] = []
        try:
            await page.goto(spec["url"], wait_until="domcontentloaded", timeout=30000)
            if urlsplit(page.url).hostname.lower() != host:
                return {"state": "BLOCKED", "reason": "Redirected away from the approved portal"}
            for field in fields:
                if urlsplit(page.url).hostname.lower() != host:
                    return {"state": "BLOCKED", "reason": "Unexpected portal navigation", "mapped_fields": completed}
                locator = page.get_by_label(field["label"], exact=True)
                if await locator.count() != 1:
                    return {"state": "REVIEW_REQUIRED", "reason": "Required label is missing or ambiguous",
                            "mapped_fields": completed, "unmapped_field": field["field"]}
                await locator.fill(field["value"])
                completed.append(field["field"])
            return {
                "state": "POPULATED_DURING_SESSION_NOT_SUBMITTED",
                "portal_host": host,
                "mapped_fields": completed,
                "submission_authorized": False,
                "persisted_as_draft": "UNKNOWN: no save or submit action performed",
            }
        except Exception as exc:
            # Do not return unredacted browser errors; they may embed private form data.
            return {"state": "REVIEW_REQUIRED", "reason": f"Browser operation failed ({type(exc).__name__})",
                    "mapped_fields": completed}
        finally:
            await context.close()
            await browser.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fill approved site fields; never submit")
    p.add_argument("--case", required=True, type=Path, help="Private JSON case input outside source repo")
    p.add_argument("--portal", required=True, type=Path, help="Vetted JSON form/host mapping")
    p.add_argument("--approve-disclosure", action="store_true",
                   help="Owner authorizes sending precisely mapped verified fields to this portal")
    args = p.parse_args(argv)
    if not args.approve_disclosure:
        json.dump({"state": "BLOCKED", "reason": "Portal-specific data disclosure has not been authorized"}, sys.stdout)
        sys.stdout.write("\n")
        return 2
    try:
        case = json.loads(args.case.read_text(encoding="utf-8"))
        spec = json.loads(args.portal.read_text(encoding="utf-8"))
        fields = validated_fields(case, spec)
        result = asyncio.run(fill_portal(spec, fields))
    except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
        # Only the exception type and a generic message are emitted.
        result = {"state": "BLOCKED", "reason": f"Invalid portal mapping or case evidence ({type(exc).__name__})"}
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result.get("state") == "POPULATED_DURING_SESSION_NOT_SUBMITTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
