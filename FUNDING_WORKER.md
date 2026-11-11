# NSIE Funding Worker — research and application preparation

**Status: draft prototype; not deployed or connected to any lender.**

This is an additive component of NSIE's existing capital-readiness architecture. No applicant records or additional databases are created in this GitHub repository.

## Current components

- funding_worker.py: OpenSOSData entity lookup, evidence controls, lender screening, redacted application handoff
- funding_discovery.py: official Simpler.Grants.gov grant search (requires API key; NOT an application submission API)
- funding_sources_virginia.json: public, non-SBA leads for Virginia rural business funding research
- refresh_public_sources.py: official-source webpage freshness checks, hashes, timestamps and page titles (no applicant information)
- portal_form_draft.py: allowlisted Playwright browser autofill of verified non-sensitive fields, with explicit portal data-disclosure authorization; does NOT submit
- submission_executor.py: separate approval-service signature verification, single-click submission from a fully populated review page, private receipt capture and hash-chained audit (prototype only)
- SUBMISSION_RUNBOOK.md: security/owner approval service contract, synthetic tests, production deployment gates
- requirements-funding-submission.txt: pinned optional browser and cryptography dependencies
- .github/workflows/funding-worker.yml: pull-request unit tests; optional weekday checks of *public* source pages after the branch is merged
- test_funding_worker.py and test_funding_integration.py: safety and discovery tests

## Commands for the authorized NSIE runtime

    python -m unittest discover -v -p 'test_*.py'
    python refresh_public_sources.py --sources funding_sources_virginia.json
    python funding_worker.py --case /secure/private_case.json --opportunities /secure/programs.json

Add --verify-registry to the last command only after the user authorizes use of a paid lookup and the runtime has OPENSOSDATA_API_KEY as a protected secret. The official single-lookup endpoint accepts entity_name and state. Queued result polling and direct state-file reconciliation are follow-up integrations.

    python funding_discovery.py --query "rural small business"

This requires SIMPLER_GRANTS_API_KEY in protected environment configuration. Its output consists of candidate funding opportunities; eligibility, current deadline and actual application status still require independent source verification. SBA sources are excluded by default for this case.

    python portal_form_draft.py --case /secure/private_case.json --portal /secure/approved_portal.json --approve-disclosure

This last command requires Playwright and Chromium in a trusted browser runtime. The portal configuration specifies an exact HTTPS host allowlist and label-to-verified-field mapping. Approval applies to disclosure of the *specific mapped fields to that portal*. Filling an input can already transmit its value through autosave: the workflow must not fill external forms before approval. Protected owner identifiers, consents, signatures, fees, and certifications are blocked. It never clicks Submit or saves lender drafts. Ephemeral session content is not guaranteed to persist.

## NSIE funding process

DISCOVER -> VERIFY -> SCREEN -> PREPARE -> APPROVAL -> SUBMIT -> TRACK

1. Discover: refresh sources, find published programs and record provenance; do not confuse grants for expansions with emergency operating cash.
2. Verify: check Secretary of State data, IRS evidence, addresses and identity matches. Registry matches do not independently prove who currently owns an LLC.
3. Screen: hard-exclude explicit no-SBA, impossible published minimums and reimbursement-only grants for immediate needs. Flag typical benchmarks rather than treating them as absolute rules.
4. Prepare: draft truthful lender narratives and budgets from verified financial documents, not projections presented as historical revenue.
5. Approval: specific authorization for credit checks, payments, disclosures, certifications and final submissions.
6. Submit/Track: prototype one-click approval-gated submission and receipt extraction **implemented but not deployed**; site-specific authenticated draft persistence, final approval signing service and existing NSIE PostgreSQL/audit integration remain missing.

## Outstanding implementation gates

- OpenSOSData and Simpler.Grants.gov secrets not connected; no paid registry lookup performed.
- Official Virginia SCC entity record not automatically reconciled yet.
- Final-click executor and private JSON receipt capture are implemented at prototype level, but no authenticated production browser session, applicant approval signer, portal-specific document uploads, or live grant/lender submission is connected.
- Existing NSIE event-bus/PostgreSQL binding and operational deployment have not been verified.
- GitHub Actions tests run on the draft PR; live portal integration and production acceptance tests are not complete.

No real applicant EIN, credit reports, owner IDs, account numbers, credentials, bank statements or private case payloads may be committed to GitHub. The public-source refresh contains only non-sensitive metadata.

Official references:
- https://api.opensosdata.com/docs/
- https://simpler.grants.gov/developers
- https://playwright.dev/python/docs/intro
