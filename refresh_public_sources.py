"""Refresh a curated list of official public funding pages, read-only.

Returns source headers, page title, digest and fetch status. Does NOT mark
program eligibility verified, bypass site blocks, or read applicant private data.
The resulting JSON is intended for ingestion by the existing NSIE evidence bus.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)


def approved_url(url: str, allowed_hosts: set[str]) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname is not None
        and parts.hostname.lower() in allowed_hosts
        and parts.username is None
        and parts.password is None
    )


class LockedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not approved_url(newurl, self.allowed_hosts):
            raise urllib.error.URLError("Redirect outside approved hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_one(source: dict, *, timeout: int = 15) -> dict:
    url = source.get("url", "")
    host = urlsplit(url).hostname
    safe_hosts = {host.lower()} if host else set()
    # A source's public URL is the only approved destination for this scan.
    if not approved_url(url, safe_hosts):
        return {"source_id": source.get("id"), "state": "BLOCKED", "reason": "Invalid HTTPS source"}
    opener = urllib.request.build_opener(LockedRedirectHandler(safe_hosts))
    request = urllib.request.Request(url, headers={"User-Agent": "NSIE-Public-Funding-Research/0.1"})
    checked = utcnow()
    try:
        with opener.open(request, timeout=timeout) as response:
            if urlsplit(response.url).hostname.lower() not in safe_hosts:
                raise urllib.error.URLError("Unexpected redirect")
            # Bounded read: no untrusted document or applicant file ingestion.
            contents = response.read(300001)
            if len(contents) > 300000:
                return {"source_id": source.get("id"), "state": "REVIEW_REQUIRED",
                        "reason": "Page exceeded scan limit", "checked_at": checked}
            parser = TitleParser()
            parser.feed(contents.decode("utf-8", errors="replace"))
            return {
                "source_id": source["id"], "state": "FETCHED_UNREVIEWED",
                "checked_at": checked, "source_url": url, "resolved_url": response.url,
                "content_sha256": hashlib.sha256(contents).hexdigest(),
                "page_title": " ".join(parser.title).strip()[:200],
                "bytes": len(contents),
                "eligibility": "NOT_ASSESSED",
                "application_open": "UNKNOWN",
            }
    except urllib.error.HTTPError as exc:
        return {"source_id": source.get("id"), "state": "BLOCKED",
                "reason": f"HTTP {exc.code}", "checked_at": checked}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"source_id": source.get("id"), "state": "BLOCKED",
                "reason": f"Source unavailable ({type(exc).__name__})", "checked_at": checked}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check official source-page freshness")
    p.add_argument("--sources", type=Path, default=Path(__file__).with_name("funding_sources_virginia.json"))
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args(argv)
    if not 0 <= args.limit <= 30:
        p.error("Limit must be 0–30")
    data = json.loads(args.sources.read_text(encoding="utf-8"))
    sources = data["sources"]
    if not isinstance(sources, list):
        p.error("Source registry must contain a sources array")
    checks = [fetch_one(s) for s in sources[:args.limit]]
    json.dump({"checked_at": utcnow(), "case_focus": data.get("case_focus"),
               "sources": checks, "writes_to_external_portals": False}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
