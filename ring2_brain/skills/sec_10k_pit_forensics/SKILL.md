---
name: sec_10k_pit_forensics
description: SEC 10-K/10-Q point-in-time forensics; statementKnowledgeTime, restatement handling, filing acceptance cutoff, and unadjusted split traps.
silo: US
tags: [sec, edgar, pit, 10-k, fundamentals]
---

# SEC 10-K/10-Q point-in-time forensics

## The hard rule
Every filing fact carries `statementKnowledgeTime` (the EDGAR acceptance
timestamp) and `validTime` (the fiscal period). A fact may enter a thesis only
when `knowledgeTime <= asOfTime`. The `sec_edgar.py` PIT guard enforces this;
do not bypass it with a fiscal-period lookup.

## Restatements and amendments
- 10-K/A and 10-Q/A replace the original for *future* knowledge times only.
  A backtest at 2024-03-01 must use the original filing even if an amendment
  lands in 2025.
- If two filings share a fiscal period, prefer the later acceptance time only
  when it is still <= asOfTime.

## Split and dividend traps
- Yahoo/Stooq close series are split-adjusted; the filing's per-share figures
  are as-reported. Reconcile the split factor before comparing EPS or book
  value. A 4:1 split makes an unadjusted EPS look 4x too high.
- Use the dual-vendor divergence gate (>1.50%) and the split guard (>=45%) to
  quarantine a mismatched series rather than silently repairing it.

## Output discipline
- Every derived multiple (P/E, EV/EBITDA, FCF yield) must carry the SHA-256
  digest of the GroundedFactPacket that supplied its inputs.
