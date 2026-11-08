---
name: crypto_funding_cascade
description: Crypto perpetual funding, basis and liquidation cascades; detects crowded leverage, funding flips, and exchange-specific liquidation clustering before a scalp.
silo: CRYPTO
tags: [crypto, funding, basis, liquidation, perp]
---

# Crypto funding and liquidation cascades

## Funding is a carry cost, not a signal by itself
- A positive perp funding rate means longs pay shorts. A crowded long with
  funding above the 30-day 90th percentile is fragile, not bullish.
- The silo cost floor must include the funding interval that straddles the
  expected holding period. A 3-second scalp can ignore funding; a 4-hour swing
  cannot.

## Cascade signatures
- Watch the three-tick MiroFish cascade: rising `crowd_asymmetry_score` with a
  negative `narrative_exhaustion_divergence` is a crowded long into exhaustion.
- A `cascade_fragility_index >= 0.75` revokes long leases immediately. Do not
  average down into a liquidation cluster.
- Exchange-specific liquidation engines cluster at round numbers and at the
  prior high/low. A wick through the cluster is often the low-liquidity fill,
  not a new trend.

## Execution
- Perp maker rebates differ by venue. The silo registry owns the tuple.
- A resting bid is never proof of liquidity: require executed taker sell flow
  at or through the peg before the shadow twin records a fill.
