# Individual Tax Return — Drafting Methodology

This methodology covers the **drafting** workflow at MC&S Pty Ltd: producing a draft client tax deductions report from source documents the client provides. It is distinct from the **review** methodology (`individual_tax_return.md`), which covers year-over-year comparison of a prior-year return against current-year inputs.

## 1. Scope and input shapes

Drafting jobs receive source documents in variable shapes depending on what the client returns:

- **Input shape A — client-authored materials.** The client provides their own materials (typically an email plus one or more spreadsheets and/or PDFs). The accountant extracts structure from the client's organisation rather than from a templated form. See `examples/joe_brzezek_fy25/` for a worked example.

- **Input shape B — completed firm checklist.** The client returns the MC&S individual checklist (see `templates/individual_checklist.docx`) with category totals filled in. Structured input; the methodology applies the same Step 1–6 process but with reduced extraction burden.

The methodology applies to both shapes. Production drafting may encounter mixed inputs (partial checklist plus supplementary client documents).

## 2. Step 1 — Source Collection and Classification

Gather all source documents the client has provided. The document types encountered are:

- PAYG summaries / STP income
- Bank loan statements
- Rental property statements
- Share sale contracts
- Invoices and receipts
- Workpapers
- Prior year returns
- Accountant emails
- Depreciation schedules
- ATO correspondence

Classify each document into one of the following categories:

- Income
- Deduction
- Capital item
- Balance sheet / ownership evidence
- Compliance evidence
- Missing information requests

## 3. Step 2 — Data Extraction

For each document, extract the following fields:

- Names/entities
- Dates
- Dollar values
- GST status
- Ownership %
- Purpose/use
- Tax classification

Per-document decision rules apply. A Bunnings receipt, for instance, requires deciding:

- Repair vs improvement
- Immediate deduction vs capital
- Rental vs private
- Div 40 vs Div 43
- Substantiation adequacy

Worked examples of source-to-output translation:

- **Source data:** "Uber trip to office"
  **Outcome:** "Non-deductible private commute."

- **Source data:** "Laptop purchased $2,400 used 70% for work"
  **Outcome:** "Eligible for depreciation claim under Div 40 based on 70% work use."

See §9 — Limitations: this layer of per-document decisions is a known thin spot of v1.

## 4. Step 3 — Cross-Checking and Reconciliation

Cross-check related document pairs to surface omissions and inconsistencies:

- Loan interest vs rental schedule
- Agent statements vs bank deposits
- PAYG income vs employer allowances
- Share sale dates vs broker summaries
- Repairs claimed vs capital works indicators

## 5. Step 4 — Tax Treatment Layer

Apply tax rules to each extracted item. Rule sources consulted:

- ITAA97
- ATO rulings
- Occupation-specific guidance
- Rental property rules
- Capital allowance treatment
- Deductibility tests under s8-1

For each item, determine the outcome:

- Deductible
- Non-deductible
- Capital
- Apportionment required
- Substantiation issue
- Audit risk item

The operational exemplar at `examples/joe_brzezek_fy25/deductions_report.docx` shows applied outcomes in the Notes columns of each D-code table — these are the working artifacts of Step 4 in practice. See §9 — Limitations: this section names the rule sources but does not encode their application to specific items.

## 6. Step 5 — Summary Construction (output structure)

The output is a Word document organised by ATO deduction codes (D1, D2, D3, D4, D5, D9, etc.). See `examples/joe_brzezek_fy25/deductions_report.docx` for the worked example. The section structure is:

- **Per-D-code tables.** Columns: Expense Item, Cost (AUD), Work %, Deductible (AUD), Method/Notes. The Notes column carries the operational substance of Step 4 — it states the deductibility decision and reasoning per line item (e.g. "Immediate deduction (<$300)", "Depreciated over 4 years", "Deductible for commission-earning salesperson", "Conventional clothing is a private expense").

- **Total Deductions Summary table.** D-code totals plus the grand total claimable, with a note that totals exclude items flagged for review.

- **Items Flagged for Review.** Issues requiring additional client information before lodgement: missing logbooks, WFH hours records, ambiguous expense purposes, etc. Format: two columns, Issue + Action Required.

- **Items NOT Claimed.** Defensive documentation of items the client provided that were *not* claimed, with a reason per item. This section is methodology-significant: it pre-empts client questions about why items they listed didn't reach the return.

- **Suggested Client Follow-Up Email.** A draft email in the accountant's voice, asking the client for the information items flagged for review.

Sections appear only when relevant to the client matter — a client with no rental property has no rental-related D-code table; a client with everything substantiated has no Items Flagged section.

## 7. Step 6 — Risk Filtering

Filter the populated return for items requiring extra scrutiny or client confirmation:

- Weak substantiation
- Unusually high claims
- Repairs/improvements risk
- Home office overclaims
- Car logbook deficiencies
- Mixed-purpose loans
- Redraw contamination

These risks surface in the Items Flagged for Review section of the output.

## 8. Voice and tone — client-facing communication

The Suggested Client Follow-Up Email section of the output adopts the accountant's voice. Refer to the email at the bottom of `examples/joe_brzezek_fy25/deductions_report.docx` for the worked example — first person, plain language, technical accuracy without jargon, kind-regards sign-off with the preparing accountant's name.

See §9 — Limitations: this methodology has one worked email exemplar.

## 9. Limitations of this methodology (v1)

This is the v1 drafting methodology document. The following are known thin spots that the v1 probe will exercise, with refinements to follow. They are deliberate v1 scope, not defects.

1. **Step 2 per-document decision rules** are gestured at (Bunnings receipt: repair vs improvement, etc.) but not fully encoded. The operational exemplar shows applied outcomes in the Notes columns; the methodology will be refined as specific decision failures surface in probe runs.

2. **Step 4 tax-treatment layer** names rule sources (ITAA97, ATO rulings, etc.) but does not encode their application to specific items. Refinement will follow probe results.

3. **Occupation-specific deductibility** is not encoded beyond what the Joe exemplar shows for commercial real estate consultants. Other occupations (nurses, tradies, teachers) will surface as their own gaps when their first probes run.

4. **Effective life / depreciation rules** are applied per ATO TR 2024/1 schedule but not encoded in this document. The methodology points to ATO TR 2024/1 as authoritative; the AI must consult that schedule for unfamiliar assets.

5. **Input-shape variance** between checklist (shape B) and client-authored materials (shape A) is documented but not exhaustively exemplified — only one shape A example (Joe) is committed. Other shape A clients may use different organisational conventions.

6. **Single email exemplar for the client communication voice.** Voice fidelity is grounded on one Joe-Brzezek-style email. Additional exemplars may be added if voice drift surfaces.

The probe-refine loop (established by the review methodology arc in Tasks B/C/C2/D) applies here: v1 produces output, gaps surface as observed failures, the methodology document refines, re-probe.
