---
name: fx_macro_blackout
description: FX macro-event blackout windows; NFP/CPI/FOMC/ECB release timing, spread blowouts, MT5 execution risk, and when to abstain.
silo: FX
tags: [fx, macro, blackout, mt5, spread]
---

# FX macro blackout windows

## Blackout calendar
- Do not initiate a new scalp in the 15 minutes before and 5 minutes after a
  tier-1 release: US NFP, US CPI, FOMC statement/presser, ECB decision, and
  the London 16:00 fix.
- Existing positions are reduced, not added. The ADR-009 reduce-only path is
  the correct tool; the kill switch is reserved for a broken venue.

## Spread and slippage
- MT5 spreads can widen 5-20x around a release. The 12 bps max-spread gate is
  a hard rejection, not a suggestion.
- Require dual-vendor agreement; a single broker quote during a blackout is a
  hypothesis. The feed quarantine drops a vendor on >1.50% divergence.

## Event risk after the print
- The first 60 seconds are liquidity vacuum; the second move is often the
  real repricing. Wait for executed taker flow to normalize before arming a
  lease.
- A macro surprise can invalidate a technical setup entirely; the committee
  must see the release timestamp in the GroundedFactPacket before publishing a
  thesis.
