"""Official US federal grant discovery for NSIE.

Searches the Simpler.Grants.gov API. It does NOT determine applicant
eligibility, submit a proposal, or assume a federal grant suits emergency
working capital. Returned records need full-source review.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

SEARCH_URL = "https://api.simpler.grants.gov/v1/opportunities/search"


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def query_grants(*, api_key: str, query: str, limit: int = 25, exclude_sba: bool = True, timeout: int = 30) -> dict[str, Any]:
    if not api_key:
        return {"state": "BLOCKED", "reason": "SIMPLER_GRANTS_API_KEY not configured"}
    if not query.strip():
        return {"state": "BLOCKED", "reason": "Nonempty search query required"}
    if not 1 <= limit <= 100:
        return {"state": "BLOCKED", "reason": "Limit must be between 1 and 100"}
    payload = {
        "query": query.strip(),
        "filters": {"opportunity_status": {"one_of": ["posted"]}},
        "pagination": {
            "page_offset": 1, "page_size": limit,
            "sort_order": [{"order_by": "post_date", "sort_direction": "descending"}],
        },
    }
    request = urllib.request.Request(
        SEARCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"state": "BLOCKED", "reason": f"Grant API HTTP {exc.code}", "checked_at": now_utc()}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"state": "BLOCKED", "reason": f"Grant API unavailable ({type(exc).__name__})", "checked_at": now_utc()}

    records = data.get("data")
    if not isinstance(records, list):
        return {"state": "REVIEW_REQUIRED", "reason": "Unexpected API response shape", "checked_at": now_utc()}
    return {"state": "DISCOVERY_COMPLETE", "checked_at": now_utc(), "provider": "Simpler.Grants.gov",
            "search_query": query, "source_endpoint": SEARCH_URL, "candidates": summarize(records, exclude_sba=exclude_sba)}


def summarize(records: list[dict[str, Any]], *, exclude_sba: bool = True) -> list[dict[str, Any]]:
    output = []
    seen = set()
    for rec in records:
        # Search results are not proof of eligibility or proof of current funding.
        agency = str(rec.get("agency_name") or "")
        agency_code = str(rec.get("agency_code") or "")
        if exclude_sba and ("small business administration" in agency.lower() or agency_code.upper() == "SBA"):
            continue
        opportunity_id = rec.get("opportunity_id")
        title = rec.get("opportunity_title")
        if not opportunity_id or not title or str(opportunity_id) in seen:
            continue
        seen.add(str(opportunity_id))
        output.append({
            "opportunity_id": str(opportunity_id),
            "title": str(title),
            "agency": agency,
            "opportunity_number": rec.get("opportunity_number"),
            "close_date_reported": rec.get("close_date"),
            "status_reported": rec.get("opportunity_status"),
            "source": SEARCH_URL,
            "primary_record_review_required": True,
            "eligible_for_business": "UNKNOWN",
            "eligible_for_emergency_cash": "UNKNOWN",
            "submitted": False,
        })
    return output


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Search federal grants; never submit")
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--include-sba", action="store_true", help="Only for applicants who permit SBA sources")
    args = p.parse_args(argv)
    response = query_grants(api_key=os.getenv("SIMPLER_GRANTS_API_KEY", ""), query=args.query, limit=args.limit, exclude_sba=not args.include_sba)
    json.dump(response, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if response.get("state") == "DISCOVERY_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
