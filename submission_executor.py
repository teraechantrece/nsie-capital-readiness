"""NSIE FundingFlow: approval-gated browser submission prototype.

The worker cannot sign its own approvals: it verifies an Ed25519 signature
from a separate owner-authenticated approval service. The private key must
never be available to this repository or runtime.

A single-host audit journal blocks duplicate clicks and uncertain retries.
Production needs the NSIE shared transactional ledger for multiple workers.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_ALLOWED_KEYS = {
    "schema_version", "application_id", "case_ref", "program", "portal_url",
    "portal_host", "review_selector", "review_sha256", "submit_button_name",
    "confirmation_selector", "confirmation_phrase", "confirmation_id_regex",
    "requested_usd", "credit_inquiry", "fee_usd", "certifications_required",
    "application_sha256", "receipt_screenshot",
}
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{8,100}$")


def canon(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canon(value)).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Approval time must include timezone")
    return parsed.astimezone(dt.timezone.utc)


def check_host(url: str, expected_host: str) -> None:
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.hostname.lower() != expected_host.lower()):
        raise ValueError("URL is not on the approved HTTPS host")


def validate_packet(packet: dict[str, Any]) -> None:
    if not isinstance(packet, dict) or set(packet) != _ALLOWED_KEYS:
        raise ValueError("Packet schema must match exactly")
    if packet["schema_version"] != 1:
        raise ValueError("Unsupported packet version")
    for key in ("application_id", "case_ref"):
        if not isinstance(packet[key], str) or not _ID.fullmatch(packet[key]):
            raise ValueError("Invalid application/case reference")
    for key in ("application_sha256", "review_sha256"):
        if not isinstance(packet[key], str) or not _HEX64.fullmatch(packet[key]):
            raise ValueError("Missing evidence/review digest")
    if not isinstance(packet["portal_host"], str) or not packet["portal_host"]:
        raise ValueError("Portal host missing")
    check_host(packet["portal_url"], packet["portal_host"])
    for key in ("program", "review_selector", "submit_button_name",
                "confirmation_selector", "confirmation_phrase", "confirmation_id_regex"):
        if not isinstance(packet[key], str) or not packet[key].strip() or len(packet[key]) > 200:
            raise ValueError("Missing or oversized portal recipe field")
    if packet["confirmation_phrase"].lower() in ("submitted", "success", "thank you"):
        raise ValueError("Confirmation phrase is too generic")
    pattern = re.compile(packet["confirmation_id_regex"])
    if pattern.groups != 1:
        raise ValueError("Receipt regex must capture exactly one confirmation ID")
    for key, limit in (("requested_usd", 1e9), ("fee_usd", 1e6)):
        v = packet[key]
        if (not isinstance(v, (int, float)) or isinstance(v, bool)
                or not 0 <= v <= limit):
            raise ValueError("Invalid amount")
    for key in ("credit_inquiry", "certifications_required", "receipt_screenshot"):
        if not isinstance(packet[key], bool):
            raise ValueError("Invalid risk flag")


def verify_approval(packet: dict[str, Any], envelope: dict[str, Any],
                    public_key_b64: str, now: dt.datetime | None = None) -> dict[str, Any]:
    """Verify a separate approval service's signed, exact application decision."""
    validate_packet(packet)
    if not public_key_b64:
        raise ValueError("External approval public key not configured")
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature_b64"}:
        raise ValueError("Invalid approval envelope")
    p = envelope["payload"]
    keys = {"schema_version", "intent_sha256", "application_id", "case_ref",
            "approval_id", "reviewer_ref", "issued_at", "expires_at",
            "action", "approved_credit_inquiry", "approved_fee_usd",
            "approved_certifications"}
    if not isinstance(p, dict) or set(p) != keys:
        raise ValueError("Invalid approval fields")
    if p["schema_version"] != 1 or p["action"] != "submit_exact_application":
        raise ValueError("Wrong approval action")
    for key in ("approval_id", "reviewer_ref"):
        if not isinstance(p[key], str) or not _ID.fullmatch(p[key]):
            raise ValueError("Verified reviewer and approval nonce required")
    if (p["intent_sha256"] != digest(packet)
            or p["application_id"] != packet["application_id"]
            or p["case_ref"] != packet["case_ref"]):
        raise ValueError("Approval does not match reviewed application")
    at = now or utcnow()
    issued, expires = parse_utc(p["issued_at"]), parse_utc(p["expires_at"])
    if issued > at or expires <= at or expires - issued > dt.timedelta(minutes=30):
        raise ValueError("Approval expired, premature or too long-lived")
    if packet["credit_inquiry"] and p["approved_credit_inquiry"] is not True:
        raise ValueError("Specific credit inquiry authorization missing")
    approved_fee = p["approved_fee_usd"]
    if (not isinstance(approved_fee, (int, float)) or isinstance(approved_fee, bool)
            or approved_fee < packet["fee_usd"]):
        raise ValueError("Application fee exceeds approved amount")
    if packet["certifications_required"] and p["approved_certifications"] is not True:
        raise ValueError("Required certifications not approved")
    if (p["approved_certifications"] not in (True, False)
            or p["approved_credit_inquiry"] not in (True, False)):
        raise ValueError("Invalid approval risk flags")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key_bytes = base64.b64decode(public_key_b64, validate=True)
        signature = base64.b64decode(envelope["signature_b64"], validate=True)
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(signature, canon(p))
    except Exception as exc:
        raise ValueError("Signature verification failed") from exc
    return p


def safe_private_root(root: Path) -> Path:
    if root.is_symlink():
        raise ValueError("Symlink private root not allowed")
    path = root.resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError("Existing private runtime directory required")
    if path.stat().st_mode & 0o077:
        raise ValueError("Private directory must have 0700 permissions")
    return path


class AuditLedger:
    """Single-host audit ledger; production needs shared PostgreSQL uniqueness."""
    def __init__(self, private_root: Path):
        self.root = safe_private_root(private_root)
        self.path = self.root / "funding_submission_events.jsonl"

    @contextmanager
    def locked(self):
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise ValueError("Audit file permissions too broad")
            fcntl.flock(fd, fcntl.LOCK_EX)
            with os.fdopen(fd, "r+", encoding="utf-8", closefd=False) as stream:
                previous = "0" * 64
                events = []
                for line in stream.readlines():
                    event = json.loads(line)
                    if event.get("prev_hash") != previous:
                        raise ValueError("Audit integrity failure")
                    without_hash = {k: v for k, v in event.items() if k != "hash"}
                    if digest(without_hash) != event.get("hash"):
                        raise ValueError("Audit integrity failure")
                    previous = event["hash"]
                    events.append(event)
                yield stream, events, previous
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write(self, stream, data: dict[str, Any], previous: str) -> None:
        record = dict(data, prev_hash=previous)
        record["hash"] = digest(record)
        stream.seek(0, os.SEEK_END)
        stream.write(canon(record).decode() + "\n")
        stream.flush()
        os.fsync(stream.fileno())

    def reserve(self, packet: dict[str, Any], approval: dict[str, Any]) -> None:
        with self.locked() as (stream, events, previous):
            if any(e.get("application_id") == packet["application_id"] for e in events):
                raise ValueError("Application already attempted; inspect before resubmission")
            if any(e.get("approval_id") == approval["approval_id"] for e in events):
                raise ValueError("Approval nonce already consumed")
            self._write(stream, {
                "state": "RESERVED_BEFORE_CLICK", "at": iso_now(),
                "application_id": packet["application_id"],
                "case_hash": text_digest(packet["case_ref"]),
                "approval_id": approval["approval_id"],
                "intent_sha256": digest(packet),
                "reviewer_hash": text_digest(approval["reviewer_ref"]),
            }, previous)

    def conclude(self, packet: dict[str, Any], approval: dict[str, Any],
                 state: str, receipt_sha256: str | None = None) -> None:
        with self.locked() as (stream, events, previous):
            if not any(e.get("state") == "RESERVED_BEFORE_CLICK"
                       and e.get("application_id") == packet["application_id"]
                       and e.get("approval_id") == approval["approval_id"] for e in events):
                raise ValueError("No reserved attempt found")
            if any(e.get("state") in ("SUBMISSION_CONFIRMED", "SUBMISSION_UNCERTAIN")
                   and e.get("application_id") == packet["application_id"] for e in events):
                raise ValueError("Submission result already recorded")
            self._write(stream, {"state": state, "at": iso_now(),
                                 "application_id": packet["application_id"],
                                 "approval_id": approval["approval_id"],
                                 "receipt_sha256": receipt_sha256}, previous)


def secure_receipt(root: Path, packet: dict[str, Any], confirmation: str,
                   page_url: str, confirmation_text: str,
                   screenshot_sha256: str | None = None) -> tuple[str, str]:
    check_host(page_url, packet["portal_host"])
    parts = urlsplit(page_url)
    sanitized_url = f"{parts.scheme}://{parts.hostname}{parts.path}"
    record = {
        "state": "SUBMISSION_CONFIRMED", "at": iso_now(),
        "application_id": packet["application_id"],
        "program": packet["program"], "portal": sanitized_url,
        "intent_sha256": digest(packet), "confirmation_id": confirmation,
        "confirmation_page_sha256": text_digest(confirmation_text),
        "screenshot_sha256": screenshot_sha256,
        "notes": "Submission receipt only; approval/award not implied",
    }
    root = safe_private_root(root)
    output = root / f'receipt_{packet["application_id"]}.json'
    fd = os.open(str(output), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(canon(record).decode() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return str(output), digest(record)


async def submit_on_review_page(page: Any, packet: dict[str, Any],
                                ledger: AuditLedger, approval_envelope: dict[str, Any],
                                public_key_b64: str) -> dict[str, Any]:
    """Exactly one click from a verified pre-filled, authenticated review page.

    Do not retry an uncertain submission. This only supports vetted portals
    with an exact stable review summary and a receipt-number confirmation.
    """
    approval = verify_approval(packet, approval_envelope, public_key_b64)
    check_host(page.url, packet["portal_host"])
    if page.url != packet["portal_url"]:
        raise ValueError("Review page URL differs from signed approval")
    review = page.locator(packet["review_selector"])
    if await review.count() != 1:
        raise ValueError("Review summary selector not unique")
    summary = await review.inner_text()
    if text_digest(summary) != packet["review_sha256"]:
        raise ValueError("Displayed terms differ from the approved version")
    button = page.get_by_role("button", name=packet["submit_button_name"], exact=True)
    if await button.count() != 1:
        raise ValueError("Final Submit button ambiguous")

    # Persistent reservation precedes the irreversible click.
    ledger.reserve(packet, approval)
    try:
        await button.click(timeout=15000)
        check_host(page.url, packet["portal_host"])
        receipt = page.locator(packet["confirmation_selector"])
        await receipt.wait_for(state="visible", timeout=15000)
        if await receipt.count() != 1:
            raise ValueError("Confirmation selector not unique")
        confirmation_text = await receipt.inner_text()
        if packet["confirmation_phrase"] not in confirmation_text:
            raise ValueError("Confirmation phrase not observed")
        match = re.search(packet["confirmation_id_regex"], confirmation_text)
        if not match:
            raise ValueError("No matching confirmation number")
        confirmation_id = match.group(1).strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{6,100}", confirmation_id):
            raise ValueError("Unexpected confirmation ID format")

        screenshot_hash = None
        if packet["receipt_screenshot"]:
            # Screenshot may contain PII; store ONLY inside approved encrypted
            # private runtime with exclusive file permissions, never in CI logs.
            screenshot = await page.screenshot(full_page=False)
            if not screenshot or len(screenshot) > 12_000_000:
                raise ValueError("Screenshot missing or oversized")
            screenshot_hash = hashlib.sha256(screenshot).hexdigest()
            shot_path = ledger.root / f'receipt_{packet["application_id"]}.png'
            fd = os.open(str(shot_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(screenshot)
                stream.flush()
                os.fsync(stream.fileno())

        path, receipt_hash = secure_receipt(
            ledger.root, packet, confirmation_id, page.url, confirmation_text, screenshot_hash)
        ledger.conclude(packet, approval, "SUBMISSION_CONFIRMED", receipt_hash)
        return {
            "state": "SUBMISSION_CONFIRMED",
            "application_id": packet["application_id"], "receipt_file": path,
            "receipt_sha256": receipt_hash, "requires_owner_followup": False,
        }
    except Exception as exc:
        # Click may have succeeded remotely. Record uncertainty; never retry.
        ledger.conclude(packet, approval, "SUBMISSION_UNCERTAIN")
        return {
            "state": "SUBMISSION_UNCERTAIN", "application_id": packet["application_id"],
            "reason": f"Portal response not confirmed ({type(exc).__name__})",
            "requires_owner_followup": True, "automatic_retry_allowed": False,
        }


async def execute(packet: dict[str, Any], envelope: dict[str, Any],
                  root: Path, public_key_b64: str,
                  session_state: Path) -> dict[str, Any]:
    verify_approval(packet, envelope, public_key_b64)
    ledger = AuditLedger(root)
    if (session_state.is_symlink() or not session_state.is_file()
            or session_state.stat().st_mode & 0o077):
        raise ValueError("Protected authenticated browser session (0600) required")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ValueError("Playwright is not installed") from exc

    async with async_playwright() as browser_driver:
        browser = await browser_driver.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=str(session_state), accept_downloads=False,
            service_workers="block")
        async def restrict_outbound(route):
            try:
                check_host(route.request.url, packet["portal_host"])
            except ValueError:
                await route.abort()
            else:
                await route.continue_()
        # Exact approved portal host only, including subresources and redirects.
        # A portal needing other origins requires separately vetted runtime policy.
        await context.route("**/*", restrict_outbound)
        page = await context.new_page()
        try:
            await page.goto(packet["portal_url"], wait_until="domcontentloaded", timeout=20000)
            return await submit_on_review_page(page, packet, ledger, envelope, public_key_b64)
        finally:
            await context.close()
            await browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NSIE one-time approved submission")
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--private-root", required=True, type=Path)
    parser.add_argument("--session-state", required=True, type=Path)
    parser.add_argument("--execute-approved-submission", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute_approved_submission:
        print(json.dumps({"state": "BLOCKED", "reason": "Executor action not enabled"}))
        return 2
    try:
        output = asyncio.run(execute(
            json.loads(args.packet.read_text(encoding="utf-8")),
            json.loads(args.approval.read_text(encoding="utf-8")),
            args.private_root,
            os.environ.get("NSIE_APPROVAL_PUBLIC_KEY_B64", ""),
            args.session_state,
        ))
    except Exception as exc:
        output = {"state": "BLOCKED", "reason": f"Failed closed ({type(exc).__name__})"}
    print(json.dumps(output))  # Never prints confirmation number or financial values.
    return 0 if output["state"] == "SUBMISSION_CONFIRMED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
