"""Unit tests for discovery and portal safety. No browser or network required."""
import unittest

from funding_discovery import query_grants, summarize
from portal_form_draft import validate_portal, validated_fields
from refresh_public_sources import approved_url, TitleParser


class DiscoveryTests(unittest.TestCase):
    def test_missing_grant_key_blocks(self):
        self.assertEqual(query_grants(api_key="", query="small business")["state"], "BLOCKED")

    def test_excludes_sba_and_duplicates_by_default(self):
        records = [
            {"opportunity_id": "1", "opportunity_title": "SBA source", "agency_name": "Small Business Administration"},
            {"opportunity_id": "2", "opportunity_title": "Research", "agency_name": "Department of Agriculture"},
            {"opportunity_id": "2", "opportunity_title": "Research duplicate", "agency_name": "Department of Agriculture"},
        ]
        self.assertEqual([r["opportunity_id"] for r in summarize(records)], ["2"])

    def test_no_claim_of_eligibility_or_submission(self):
        output = summarize([{"opportunity_id": "3", "opportunity_title": "Program", "agency_name": "USDA"}])[0]
        self.assertEqual(output["eligible_for_business"], "UNKNOWN")
        self.assertFalse(output["submitted"])


class BrowserDraftTests(unittest.TestCase):
    def setUp(self):
        self.spec = {
            "url": "https://funding.example.org/apply", "allowed_hosts": ["funding.example.org"],
            "mappings": [{"field": "business_name", "label": "Business name"}],
        }
        self.case = {"fields": {"business_name": {
            "value": "Example Tea Cafe", "evidence_status": "verified", "source_ref": "official-record"
        }}}

    def test_only_https_allowlisted_portal(self):
        self.assertEqual(validate_portal(self.spec), "funding.example.org")
        self.spec["url"] = "http://funding.example.org/apply"
        with self.assertRaises(ValueError):
            validate_portal(self.spec)

    def test_portal_host_mismatch_blocks(self):
        self.spec["url"] = "https://unrelated.example.org/apply"
        with self.assertRaises(ValueError):
            validate_portal(self.spec)

    def test_verified_business_name_can_be_mapped(self):
        self.assertEqual(validated_fields(self.case, self.spec)[0]["value"], "Example Tea Cafe")

    def test_no_unverified_data(self):
        self.case["fields"]["business_name"]["evidence_status"] = "reported"
        with self.assertRaises(ValueError):
            validated_fields(self.case, self.spec)

    def test_private_ein_never_autofilled(self):
        self.spec["mappings"][0] = {"field": "ein", "label": "Tax EIN"}
        self.case["fields"]["ein"] = {"value": "REDACTED", "evidence_status": "verified", "source_ref": "private"}
        with self.assertRaises(ValueError):
            validated_fields(self.case, self.spec)

    def test_certification_and_consent_cannot_be_autofilled(self):
        for label in ("Certification statement", "I agree to credit check", "Authorize guaranty"):
            self.spec["mappings"][0]["label"] = label
            with self.subTest(label=label), self.assertRaises(ValueError):
                validated_fields(self.case, self.spec)


class PublicSourceTests(unittest.TestCase):
    def test_rejects_mixed_content_or_other_hosts(self):
        allow = {"www.example.gov"}
        self.assertTrue(approved_url("https://www.example.gov/loans", allow))
        self.assertFalse(approved_url("http://www.example.gov/loans", allow))
        self.assertFalse(approved_url("https://evil.example.gov/loans", allow))
        self.assertFalse(approved_url("https://user:pass@www.example.gov/loans", allow))

    def test_html_title_extraction(self):
        p = TitleParser()
        p.feed("<html><head><title>Official Loan Program</title></head><body>content</body></html>")
        self.assertEqual(" ".join(p.title), "Official Loan Program")


if __name__ == "__main__":
    unittest.main()
