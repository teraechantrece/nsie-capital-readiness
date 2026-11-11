"""Approval and submission safety tests. No real browser, lender, or network."""
import asyncio
import base64
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from submission_executor import (
    AuditLedger, canon, digest, text_digest, validate_packet,
    verify_approval, submit_on_review_page, safe_private_root,
)

REVIEW = "Application for Test Grant\nOrganization: Sample LLC\nAmount: $0"


def packet_fixture():
    return {
        "schema_version": 1, "application_id": "APP_TEST_10001",
        "case_ref": "CASE_TEST_10001", "program": "SIMULATED PROGRAM",
        "portal_url": "https://apply.example.org/review",
        "portal_host": "apply.example.org", "review_selector": "#review",
        "review_sha256": text_digest(REVIEW),
        "submit_button_name": "Submit application",
        "confirmation_selector": "#confirmation",
        "confirmation_phrase": "Application successfully submitted",
        "confirmation_id_regex": r"Reference: ([A-Z0-9-]{8,30})",
        "requested_usd": 0, "credit_inquiry": False, "fee_usd": 0,
        "certifications_required": False, "application_sha256": "a" * 64,
        "receipt_screenshot": False,
    }


def signed(packet, private_key, now=None, **changes):
    now = now or dt.datetime.now(dt.timezone.utc)
    approval = {
        "schema_version": 1, "intent_sha256": digest(packet),
        "application_id": packet["application_id"],
        "case_ref": packet["case_ref"],
        "approval_id": "OWNER_APPROVAL_1001",
        "reviewer_ref": "OWNER_VERIFIED_1001",
        "issued_at": (now - dt.timedelta(minutes=1)).isoformat(),
        "expires_at": (now + dt.timedelta(minutes=5)).isoformat(),
        "action": "submit_exact_application",
        "approved_credit_inquiry": False, "approved_fee_usd": 0,
        "approved_certifications": False,
    }
    approval.update(changes)
    return {"payload": approval,
            "signature_b64": base64.b64encode(private_key.sign(canon(approval))).decode()}


class SimulatedLocator:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind

    async def count(self):
        return 1

    async def inner_text(self):
        return self.page.review if self.kind == "review" else self.page.confirmation

    async def wait_for(self, **kwargs):
        if self.page.missing_confirmation:
            raise RuntimeError("No confirmation returned")

    async def click(self, **kwargs):
        self.page.clicks += 1
        if self.page.redirect:
            self.page.url = "https://unapproved.example.org/confirmation"


class SimulatedPage:
    def __init__(self, packet):
        self.url = packet["portal_url"]
        self.review = REVIEW
        self.confirmation = "Application successfully submitted\nReference: ABCD123456"
        self.clicks = 0
        self.missing_confirmation = False
        self.redirect = False

    def locator(self, selector):
        return SimulatedLocator(self, "review" if selector == "#review" else "confirmation")

    def get_by_role(self, role, name, exact):
        return SimulatedLocator(self, "button")

    async def screenshot(self, **kwargs):
        return b"SIMULATED_SCREENSHOT"


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.chmod(self.root, 0o700)
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key = base64.b64encode(self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        self.packet = packet_fixture()
        self.envelope = signed(self.packet, self.private_key)
        self.approval = verify_approval(self.packet, self.envelope, self.public_key)
        self.ledger = AuditLedger(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_verified_signed_approval(self):
        self.assertEqual(self.approval["application_id"], self.packet["application_id"])

    def test_unsigned_approval_rejected(self):
        self.envelope["signature_b64"] = base64.b64encode(b"fake").decode()
        with self.assertRaises(ValueError):
            verify_approval(self.packet, self.envelope, self.public_key)

    def test_changed_application_rejected(self):
        self.packet["requested_usd"] = 25000
        with self.assertRaises(ValueError):
            verify_approval(self.packet, self.envelope, self.public_key)

    def test_expired_approval_rejected(self):
        envelope = signed(self.packet, self.private_key,
                          dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
        with self.assertRaises(ValueError):
            verify_approval(self.packet, envelope, self.public_key)

    def test_credit_pull_requires_separate_flag(self):
        self.packet["credit_inquiry"] = True
        with self.assertRaisesRegex(ValueError, "credit inquiry"):
            verify_approval(self.packet, signed(self.packet, self.private_key), self.public_key)

    def test_fee_requires_specific_approval(self):
        self.packet["fee_usd"] = 80
        with self.assertRaisesRegex(ValueError, "fee exceeds"):
            verify_approval(self.packet, signed(self.packet, self.private_key), self.public_key)

    def test_certification_requires_specific_approval(self):
        self.packet["certifications_required"] = True
        with self.assertRaisesRegex(ValueError, "certifications"):
            verify_approval(self.packet, signed(self.packet, self.private_key), self.public_key)

    def test_wrong_portal_host_rejected(self):
        self.packet["portal_url"] = "https://evil.example.org/review"
        with self.assertRaises(ValueError):
            validate_packet(self.packet)

    def test_confirmation_receipt_and_audit(self):
        page = SimulatedPage(self.packet)
        result = asyncio.run(submit_on_review_page(page, self.packet, self.ledger, self.envelope, self.public_key))
        self.assertEqual(result["state"], "SUBMISSION_CONFIRMED")
        self.assertEqual(page.clicks, 1)
        receipt = json.loads(Path(result["receipt_file"]).read_text())
        self.assertEqual(receipt["confirmation_id"], "ABCD123456")
        self.assertEqual(Path(result["receipt_file"]).stat().st_mode & 0o077, 0)
        with self.ledger.locked() as (_, events, __):
            self.assertEqual([e["state"] for e in events],
                             ["RESERVED_BEFORE_CLICK", "SUBMISSION_CONFIRMED"])
            self.assertNotIn("ABCD123456", json.dumps(events))

    def test_one_click_no_replay(self):
        asyncio.run(submit_on_review_page(SimulatedPage(self.packet),
                                          self.packet, self.ledger, self.envelope, self.public_key))
        second_page = SimulatedPage(self.packet)
        with self.assertRaisesRegex(ValueError, "already attempted"):
            asyncio.run(submit_on_review_page(second_page, self.packet,
                                              self.ledger, self.approval))
        self.assertEqual(second_page.clicks, 0)

    def test_uncertain_state_never_retries(self):
        page = SimulatedPage(self.packet)
        page.missing_confirmation = True
        outcome = asyncio.run(submit_on_review_page(page, self.packet,
                                                     self.ledger, self.approval))
        self.assertEqual(outcome["state"], "SUBMISSION_UNCERTAIN")
        self.assertEqual(page.clicks, 1)
        with self.assertRaises(ValueError):
            asyncio.run(submit_on_review_page(SimulatedPage(self.packet),
                                              self.packet, self.ledger, self.envelope, self.public_key))

    def test_review_modified_blocks_before_click(self):
        page = SimulatedPage(self.packet)
        page.review = "Changed terms $30000"
        with self.assertRaisesRegex(ValueError, "differ"):
            asyncio.run(submit_on_review_page(page, self.packet, self.ledger, self.envelope, self.public_key))
        self.assertEqual(page.clicks, 0)

    def test_external_redirect_is_uncertain(self):
        page = SimulatedPage(self.packet)
        page.redirect = True
        result = asyncio.run(submit_on_review_page(page, self.packet,
                                                    self.ledger, self.approval))
        self.assertEqual(result["state"], "SUBMISSION_UNCERTAIN")

    def test_audit_tamper_prevents_new_click(self):
        self.ledger.reserve(self.packet, self.approval)
        with self.ledger.path.open("a") as stream:
            stream.write('{"tampered":true}\n')
        with self.assertRaises(ValueError):
            self.ledger.reserve(self.packet, self.approval)

    def test_private_root_must_be_protected(self):
        os.chmod(self.root, 0o755)
        with self.assertRaises(ValueError):
            safe_private_root(self.root)

    def test_opt_in_private_screenshot(self):
        self.packet["receipt_screenshot"] = True
        self.envelope = signed(self.packet, self.private_key)
        page = SimulatedPage(self.packet)
        result = asyncio.run(submit_on_review_page(page, self.packet, self.ledger, self.envelope, self.public_key))
        self.assertEqual(result["state"], "SUBMISSION_CONFIRMED")
        path = self.root / f'receipt_{self.packet["application_id"]}.png'
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(path.read_bytes(), b"SIMULATED_SCREENSHOT")


if __name__ == "__main__":
    unittest.main()
