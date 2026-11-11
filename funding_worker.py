"""NSIE funding pre-application worker: no submissions, credit pulls or data storage.

Reads case input from a PRIVATE runtime location and emits a machine-readable,
redacted work queue for the NSIE event bus. NEVER commit case input to Git.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API_URL = "https://api.opensosdata.com/v1/lookup"


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def normalize_name(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"\b(llc|l\.?l\.?c\.?|inc|incorporated|corporation|corp|co)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def registry_result(payload: dict[str, Any], expected_name: str) -> dict[str, Any]:
    """Never infer current ownership or final legal match from fuzzy names."""
    data = payload.get("data") or {}
    returned_name = str(data.get("entityName") or "")
    name_match = bool(returned_name) and normalize_name(returned_name) == normalize_name(expected_name)
    return {
        "state": "ENTITY_NAME_MATCH" if name_match else "REVIEW_REQUIRED",
        "verified_through": "OpenSOSData live state-portal lookup (provider-reported)",
        "checked_at": timestamp(),
        "reported_source": payload.get("source", "not provided"),
        "stale": payload.get("stale"),
        "entity_name": returned_name,
        "entity_id": data.get("entityId"),
        "entity_status": data.get("status"),
        "formation_date": data.get("formationDate"),
        "principal_address": data.get("principalAddress"),
        "name_match": name_match,
        "current_ownership": "UNVERIFIED — do not infer from IRS responsible party or registered agent",
        "primary_source_check": "https://cis.scc.virginia.gov/EntitySearch/Index",
    }


def lookup_registry(name: str, state: str, api_key: str, *, timeout: int = 45) -> dict[str, Any]:
    """Paid lookup by explicit invocation only. No retries/billing loops."""
    if not api_key:
        return {"state": "BLOCKED", "reason": "OPENSOSDATA_API_KEY is not configured"}
    if not re.fullmatch("[A-Z]{2}", state.upper()):
        return {"state": "BLOCKED", "reason": "State must be a two-letter code"}
    body = json.dumps({"entity_name": name, "state": state.upper(), "source": "live"}).encode()
    request = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = response.status
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # Do not echo potentially sensitive response bodies or credentials.
        return {"state": "BLOCKED", "reason": f"Registry HTTP {exc.code}", "checked_at": timestamp()}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"state": "BLOCKED", "reason": f"Registry unavailable ({type(exc).__name__})", "checked_at": timestamp()}
    if code == 202:
        return {"state": "PENDING", "job_id": payload.get("jobId") or payload.get("job_id"), "checked_at": timestamp()}
    if code == 200 and payload.get("success"):
        return registry_result(payload, name)
    return {"state": "REVIEW_REQUIRED", "reason": "Unexpected lookup result", "checked_at": timestamp()}


def evidence_is_valid(value: Any) -> bool:
    """Evidence needs a citeable source; assertions are not auto-verified."""
    return isinstance(value, dict) and bool(value.get("value")) and value.get("evidence_status") == "verified" and bool(value.get("source_ref"))


def screen_opportunity(case: dict[str, Any], opp: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    unknowns: list[str] = []
    program = opp.get("name", "Unnamed opportunity")
    source = opp.get("official_url")
    if not source or not str(source).startswith("https://"):
        unknowns.append("Official program URL requires verification")
    if not opp.get("terms_checked_at"):
        unknowns.append("Current eligibility and deadline not verified")
    if case.get("exclude_sba", False) and opp.get("sba_backed") is True:
        reasons.append("Explicit applicant exclusion: SBA financing")
    state = str(case.get("state", "")).upper()
    states = opp.get("states")
    if states and state and state not in states:
        reasons.append("Outside stated service area")
    required_credit = opp.get("minimum_credit_score")
    reported_credit = case.get("reported_credit_range")
    if required_credit is not None and reported_credit:
        if int(reported_credit[1]) < int(required_credit):
            reasons.append("Reported credit range below stated minimum")
        elif int(reported_credit[0]) < int(required_credit):
            unknowns.append("Exact credit score must be confirmed")
    if required_credit is not None and not reported_credit:
        unknowns.append("Credit criteria not evaluated")
    amount = case.get("requested_amount")
    if amount is not None:
        if opp.get("minimum_amount") is not None and amount < opp["minimum_amount"]:
            reasons.append("Requested amount below program minimum")
        if opp.get("maximum_amount") is not None and amount > opp["maximum_amount"]:
            reasons.append("Requested amount exceeds program maximum")
    need_days = case.get("funding_needed_within_days")
    days = opp.get("published_disbursement_days")
    if days is not None and need_days is not None and days > need_days:
        reasons.append("Published funding timeline exceeds emergency deadline")
    elif days is None and need_days is not None:
        unknowns.append("Funding disbursement timeline not verified")
    if opp.get("reimbursement_only") and case.get("needs_upfront_cash"):
        reasons.append("Reimbursement grant cannot supply immediate cash")
    if reasons:
        status = "EXCLUDE"
    elif unknowns:
        status = "RESEARCH_REQUIRED"
    else:
        status = "SCREENED_NOT_APPROVED"
    return {"name": program, "state": status, "reasons": reasons, "unknowns": unknowns,
            "official_url": source, "reviewed_at": timestamp()}


def prepare_handoff(case: dict[str, Any], screens: list[dict[str, Any]], registry: dict[str, Any]) -> dict[str, Any]:
    evidence = case.get("fields", {})
    verified = {k: v["value"] for k, v in evidence.items() if evidence_is_valid(v)}
    missing = sorted(set(case.get("required_application_fields", [])) - verified.keys())
    legal_match = registry.get("state") == "ENTITY_NAME_MATCH"
    blocks = []
    if not legal_match:
        blocks.append("Current registry identity is not verified")
    if missing:
        blocks.append("Required applicant fields are unverified")
    if not case.get("cashflow_verified", False):
        blocks.append("Cash flow / ability to repay not validated")
    return {
        "case_id": case.get("case_id", "unspecified"), "generated_at": timestamp(),
        "workflow": "RESEARCH > VERIFY > SCREEN > DRAFT > HUMAN_APPROVAL > SUBMIT > TRACK",
        "verified_public_fields": verified, "missing_fields": missing,
        "registry": registry, "opportunities": screens,
        "application_state": "PREPARE_ONLY" if blocks else "READY_FOR_OWNER_REVIEW",
        "submission_authorized": False, "credit_pull_authorized": False,
        "blocking_items": blocks,
        "next_work_for_agent": [
            "Research public legal identity and check the official registry record",
            "Confirm live program eligibility, deadline and fees from primary sources",
            "Collect missing evidence from authorized private sources",
            "Prepare but DO NOT submit lender forms or grant certifications",
            "Submit only after owner approval of exact terms, credit inquiry and certifications",
            "Capture receipt/status and follow-up without claiming an unconfirmed submission",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NSIE stateless funding pre-application worker")
    parser.add_argument("--case", required=True, type=Path, help="PRIVATE applicant JSON path")
    parser.add_argument("--opportunities", required=True, type=Path, help="Curated opportunity JSON array")
    parser.add_argument("--verify-registry", action="store_true", help="Allow one paid OpenSOSData lookup")
    args = parser.parse_args(argv)
    case = json.loads(args.case.read_text(encoding="utf-8"))
    opps = json.loads(args.opportunities.read_text(encoding="utf-8"))
    if not isinstance(opps, list):
        parser.error("--opportunities must be a JSON array")
    registry = {"state": "NOT_CHECKED", "reason": "Live verification not requested"}
    if args.verify_registry:
        registry = lookup_registry(case.get("legal_name", ""), case.get("state", ""), os.getenv("OPENSOSDATA_API_KEY", ""))
    result = prepare_handoff(case, [screen_opportunity(case, o) for o in opps], registry)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
