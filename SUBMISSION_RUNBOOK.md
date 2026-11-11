# FundingFlow approval-based submission — deployment/runbook

**Current status: draft feature, not live. No customer application has been submitted.**

This executor handles the *final approved click on a portal's already-populated, authenticated review page*. It does not login, enter owner credentials, upload documents, sign certifications, pay fees, bypass CAPTCHA, or assert a lender/grant application is eligible. Those actions require separate vetted workflows and permissions.

## Trust architecture

1. **NSIE Intake/Browser draft worker (unprivileged):** collect verified fields and prepare a private lender-specific application draft. Request separate permission before disclosing personal/financial information even in fields that autosave.
2. **Review service (independently authenticated):** show the *exact* lender, URL, request amount, fees, credit inquiry, certifications and rendered final review page. Require the actual legal applicant/authorized officer to approve using an authenticated session with MFA. Record their explicit approval.
3. **Approval signing service (separate identity and key custody):** create a signed Ed25519 approval envelope. Private signing key must be in an external protected approval service/HSM; never in GitHub, browser worker, environment of this executor, or the AI agent. Include a single-use approval_id, issued/expires timestamps (at most 30 minutes), and SHA-256 digest of the exact application packet.
4. **Submission executor (this PR):** verify signature with a PUBLIC key, check portal HTTPS host and exact review-page URL, compute hash of displayed application summary and compare to approved hash, reserve attempt in audit ledger, click exact 'Submit' button **once**, then verify a specific confirmation phrase AND confirmation number on an approved host.
5. **Evidence/receipt:** in private 0700 storage, save a 0600 receipt JSON with the portal reference number, time, content hash and approval digest. Screenshot capture can be opt-in and must use an encrypted private volume; screenshots can contain sensitive applicant data. The audit chain stores *hashes and state changes* but no full financial values or receipt numbers.
6. **Uncertainty:** Any click failure, unexpected redirect, missing confirmation, receipt error, or timeout after reservation means SUBMISSION_UNCERTAIN. Never automatically retry. Reconcile manually with the portal/lender before any separately approved new attempt.

## What signing authorizes

Exact packet fields: application_id, case_ref, program, portal_url + portal_host, amount, fee, credit inquiry, certifications_required, private application_sha256, final review_sha256, final submit button name and exact success receipt matcher. The signer returns an Ed25519 envelope:

    {
      "payload": {
        "schema_version": 1,
        "intent_sha256": "<sha256 of canonical packet>",
        "application_id": "APP_TEST_10001",
        "case_ref": "CASE_TEST_10001",
        "approval_id": "OWNER_APPROVAL_1001",
        "reviewer_ref": "OWNER_VERIFIED_1001",
        "issued_at": "<ISO UTC>",
        "expires_at": "<ISO UTC within 30 minutes>",
        "action": "submit_exact_application",
        "approved_credit_inquiry": false,
        "approved_fee_usd": 0,
        "approved_certifications": false
      },
      "signature_b64": "<Ed25519 signature by external approval service>"
    }

The worker cannot generate this signature. A chat instruction, environment variable, or command-line switch is not a substitute for actual authenticated applicant authorization. The signed fields cannot change between approval and click. Credit inquiries, fees, and legally consequential certifications require independent explicit flags; any actual signature or portal-specific identity task still requires direct applicant participation.

## Runtime invocation (not a request for the owner to operate a terminal)

Trusted NSIE runtime must use Python 3.11+, cryptography 46.0.4 and Playwright 1.57.0 with Chromium. All packet/approval/session input files must be in approved private storage and must not be committed to source control.

    export NSIE_APPROVAL_PUBLIC_KEY_B64=<external signer public key>
    python submission_executor.py \
      --packet /secure/application_packet.json \
      --approval /secure/owner_signed_approval.json \
      --session-state /secure/portal_browser_state.json \
      --private-root /secure/receipts_and_audit \
      --execute-approved-submission

Requirements:
- `/secure/receipts_and_audit` must already exist, be an encrypted private volume in production and have mode 0700.
- Browser storage state holds authentication credentials; file must be mode 0600. No copy to GitHub Actions artifacts or public logs.
- No general web access: browser network traffic defaults to one explicit approved HTTPS host; any vendor that needs another origin requires review and a new signed site-specific policy.
- Review selector must summarize all material application terms. If the form lacks a stable complete review step, do NOT use this executor on that portal.
- The control flow must be invoked by the NSIE workflow engine after a proper owner approval; never expose a public endpoint that lets a user or model invoke a raw signed packet without authorization checks and rate limiting.

## Audit and idempotency

The prototype ledger is a local, locked, hash-chained JSONL journal under the private runtime directory. It is **single-host only**: a crash or ambiguous attempt leaves the application locked out to prevent replay. Local hash-chains do not protect against complete file deletion or privileged tampering. For production, bind reservation and append-only events to the existing NSIE PostgreSQL/event bus with database-level unique constraints, restrictive permissions, transactionally locked idempotency keys and externally anchored audit hash. Do not make this file journal a separate NSIE database.

Do not merge/deploy without: trusted approval-service implementation; current owner identity validation and consent workflow; actual public company verification; application draft persistence; portal adapters; real-browser integration testing on synthetic/test portals; shared transactional ledger; encrypted private receipt store; monitoring and operational incident runbook.

## Tests

The PR includes simulated portal tests that confirm signature and deadline checks, changed-terms rejection, host restrictions, single click, audit chain integrity, duplicate attempts, the SUBMISSION_UNCERTAIN path, and private receipt generation. These tests do not demonstrate live lender portal compatibility or portal permission for automation.

Sources for browser API behavior: https://playwright.dev/python/docs/api/class-page and https://playwright.dev/python/docs/api/class-locator
