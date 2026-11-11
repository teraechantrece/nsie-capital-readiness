# NSIE Funding Worker — governed pre-application component

**Status: prototype, NOT deployed.** This is a read-only funding screener and registry adapter, not a fully autonomous browser agent. It is an additive component for the existing NSIE architecture, not a new data store.

## What it does
- Queries OpenSOSData's \`POST /v1/lookup\` on explicit invocation with the API key stored as a runtime secret.
- Separates name-match evidence from **current beneficial ownership**, which is never inferred from public registry entries or an IRS EIN notice.
- Screens opportunities against user policy (e.g., no SBA), published thresholds, geographic restrictions, reimbursement rules, and emergency disbursement timing.
- Produces structured JSON ready for ingestion by NSIE's **existing event bus/PostgreSQL evidence store**. It does NOT independently persist case data.
- Prepares a controlled handoff. It cannot authorize credit checks, send bank or identity records, certify lender applications, click Submit, or commit fees.

## Local verification
\`\`\`bash
python -m unittest test_funding_worker.py -v
python funding_worker.py --case /private/case.json --opportunities /private/opportunities.json
\`\`\`

An optional *paid* registry lookup is performed only if \`--verify-registry\` is included and \`OPENSOSDATA_API_KEY\` is configured securely in the runtime.

## Operating contract
1. Public-record research is the agent's responsibility. Never ask the applicant for public record facts before attempting a source search.
2. Keep private financial documents and legal identity fields out of Git and out of logs. The business-name example in documentation must remain fictional.
3. Government record or other authoritative primary-source links, fetch date, document/version, and evidence status accompany every field.
4. An ambiguous legal-name match, missing source URL, non-current lender criteria, incomplete cash-flow evidence, or ownership unknown must trigger a review gate.
5. Never represent proposed prices, modelled revenue, or a reported credit score as verified operational data.
6. Use the applicant's explicit exclusions (including no SBA financing) as hard blocks; avoid hard credit pulls or paid applications without specific owner authorization.
7. This worker does not browse grant portals, upload confidential documents, or submit forms. The future browser connector (e.g., TinyFish in ChatGPT Work or another authorized browser runtime) must implement those interactions behind the approval gate.
8. Do not claim a program is accepting applications, will approve credit, or will fund within 7 days without checked official evidence.

## Input shapes (illustrative, not a live applicant)
Case JSON:
\`\`\`json
{"case_id":"case-demo","legal_name":"Example Tea Cafe LLC","state":"VA","exclude_sba":true,"reported_credit_range":[560,619],"requested_amount":15000,"funding_needed_within_days":7,"needs_upfront_cash":true,"cashflow_verified":false,"fields":{},"required_application_fields":["legal_name","physical_address","annual_revenue"]}
\`\`\`
Opportunity JSON:
\`\`\`json
[{"name":"Example Regional Working Capital Program","official_url":"https://example.org/loans","terms_checked_at":null,"states":["VA"],"sba_backed":false,"minimum_credit_score":null,"reimbursement_only":false,"published_disbursement_days":null}]
\`\`\`

## Next integration work (not yet implemented)
- Scheduled official funding-source discovery and freshness checks
- OpenSOSData async job polling, and direct Virginia SCC evidence cross-check
- OCR/document extraction and reconciliation from authorized sources
- Human-reviewed browser form filling with upload receipt and application-state tracking
- Event-bus/PostgreSQL binding, deployment secrets, rate/cost controls, and full end-to-end tests

**Do not upload user documents or API keys to this repository.**

Official integration documentation: https://api.opensosdata.com/docs/
