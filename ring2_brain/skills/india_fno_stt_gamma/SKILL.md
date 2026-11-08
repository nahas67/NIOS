---
name: india_fno_stt_gamma
description: India F&O edge must clear 5.5 bps STT/fee floor plus dealer-gamma pin risk before any scalp; models expiry-day gamma flips and Dhan/Zerodha margin drift.
silo: INDIA
tags: [india, fno, stt, gamma, expiry]
---

# India F&O: STT, statutory floor and dealer gamma

## Cost floor (never negotiate below it)
- Statutory STT on equity delivery is 0.1% both sides; on intraday F&O the
  effective round-trip tax + exchange fee + stamp duty floor is **5.5 bps** in
  the silo cost table. The conformal gate must subtract it before the Net_LB
  test.
- Brokerage is zero for delivery but not for F&O; the silo registry owns the
  authoritative maker/taker/STT tuple. Never hard-code a lower floor.

## Expiry-day dealer gamma
- When the NIFTY/BANKNIFTY spot sits within 0.35% of a strike with large open
  interest, dealer hedging can pin the tape. A positive `dealer_gamma_flip`
  proximity in the MiroFish shock vector is a warning, not a buy signal.
- A long scalp requires executed taker buy ratio >= 0.65 and cancel-to-fill
  < 3.0 after the pin window. Resting depth at the strike is spoofable.

## Failure modes
- STT is charged on the sell side even when the strategy loses; a +4 bps gross
  scalp is a guaranteed net loss after the 5.5 bps floor.
- Expiry-week margin spikes can freeze the silo; defer to the SiloGuard freeze
  reason rather than retrying the order.
