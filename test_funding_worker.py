import unittest
from funding_worker import normalize_name, registry_result, screen_opportunity, prepare_handoff


class FundingWorkerTests(unittest.TestCase):
    def test_punctuation_normalized_but_distinct_names_not_conflated(self):
        self.assertEqual(normalize_name("Tea & Thyme, LLC"), normalize_name("Tea and Thyme"))
        self.assertNotEqual(normalize_name("Tea and Thyme Cafe"), normalize_name("Tea and Thyme LLC"))

    def test_irs_owner_never_publicly_verified(self):
        result = registry_result({"data": {"entityName": "Sample Tea LLC", "entityId": "C123", "status": "Active"}}, "Sample Tea LLC")
        self.assertEqual(result["state"], "ENTITY_NAME_MATCH")
        self.assertIn("UNVERIFIED", result["current_ownership"])

    def test_sba_exclusion(self):
        output = screen_opportunity({"state": "VA", "exclude_sba": True}, {"name": "SBA loan", "sba_backed": True})
        self.assertEqual(output["state"], "EXCLUDE")

    def test_outside_credit_threshold(self):
        output = screen_opportunity({"state": "VA", "reported_credit_range": [560, 619]}, {"name": "Microloan", "minimum_credit_score": 650})
        self.assertEqual(output["state"], "EXCLUDE")

    def test_reimbursement_is_not_emergency_cash(self):
        output = screen_opportunity({"state": "VA", "needs_upfront_cash": True}, {"name": "Match", "reimbursement_only": True})
        self.assertEqual(output["state"], "EXCLUDE")

    def test_missing_timeline_is_not_promised(self):
        output = screen_opportunity({"state": "VA", "funding_needed_within_days": 7}, {"name": "Fund", "official_url": "https://example.org", "terms_checked_at": "2026-11-11"})
        self.assertEqual(output["state"], "RESEARCH_REQUIRED")

    def test_not_submit_without_verified_case(self):
        case = {"case_id": "example", "fields": {"name": {"value": "Example", "evidence_status": "self_reported"}}, "required_application_fields": ["name", "revenue"]}
        handoff = prepare_handoff(case, [], {"state": "NOT_CHECKED"})
        self.assertFalse(handoff["submission_authorized"])
        self.assertEqual(handoff["application_state"], "PREPARE_ONLY")


if __name__ == "__main__":
    unittest.main()
