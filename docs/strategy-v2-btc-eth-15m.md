# Strategy review: BTC/ETH 15-minute markets (Paper Engine v0.2)

Status: proposal. Paper money only. Nothing here places live orders.

This document reviews the proposed "maker-based complete-set accumulation"
plan, compares it with the strategy currently in this repo
(`core/avellaneda.py` + `selection/scoring.py`), and proposes the strategy
the v0.2 paper engine should test.

---

## 1. Verdict

1. **Complete-set accumulation and two-sided market making are the same
   trade.** On Kalshi, a NO bid at 48¢ is a YES offer at 52¢. "YES bid 46 /
   NO bid 48" is a 46/52 YES quote with a 6¢ spread. The plan's
   ledger (pairs, unmatched legs) is better than the current bot's, but it
   doesn't add a new source of edge.
2. **The edge or loss is set by adverse selection, not by spread size.**
   KXBTC15M settles on the CF Benchmarks BRTI: the 60-second average before
   close against the 60-second average before the previous close. So the
   fair price of a contract follows BTC spot on Coinbase, Kraken, Bitstamp
   and the other exchanges in the index, not the Kalshi book. Anyone watching
   spot sees a move first and trades against your stale bid. **The leg that
   fills is usually the leg that was wrong.** That is the "failed hedge",
   and it's where the losses come from.
3. **The current bot can't trade these markets profitably as written.** It
   centres quotes on the Kalshi book mid, polls REST every 5 seconds, and
   its `T`/`sigma`/`k` have nothing to do with a 15-minute contract (see §3).
4. **What could make money:** quote around a *spot-derived fair value*,
   cancel as soon as spot moves, quote mostly early in each window, and stop
   well before the final 90 seconds. Even then, success isn't assured: we
   compete for queue position with professional market makers who are faster
   than a cloud box. The paper engine's first job is to find out cheaply
   whether there's any edge. **Markouts** (§6) answer that within days,
   long before 21 days or 200 pairs.

---

## 2. Review of the proposed plan

### What is right

- No taker entries. Buying both asks always costs ≥ $1 because
  YES ask = 1 − NO bid. The plan already says this correctly.
- Dollar-denominated limits that include resting orders and fees. The
  current bot caps only contract counts.
- Marking open positions conservatively, and not assuming an unmatched leg
  can be sold at a good price.
- Forward paper testing with frozen parameters and an out-of-sample period
  before any real money; then $50 first.
- The statement that the drawdown breaker limits new trading, not the
  maximum loss.

### What to change

| Item | Proposed | Recommended | Why |
|---|---|---|---|
| Max order value | $10 / side | **$5 / side** | One unmatched $10 leg in a binary can go to $0 and use the whole $10 daily stop in one event. A worst-case single failure should be ≤ half the daily stop. |
| Max unmatched exposure | $10 | **$5** | Same reason. |
| No-quote window | final 90 s | **final 180 s to start**; test 120 s / 90 s in paper | Stale-quote risk grows like 1/√(time left) (table below). With 90 s left, a 1-second delay costs ~4¢ per 1σ spot move, which is more than the spread. |
| 20 s unmatched timeout → "reassess" | not defined | Define it (§5.4): reprice the missing leg passively up to pair break-even; then **hold to settlement**; cross only when a risk limit forces it | Crossing costs the spread plus ~1.75¢ taker fee. If the fair-value model is right, holding has an expected value equal to fair value at no extra cost. Cap the variance with the $5 unmatched limit, not with taker exits. |
| Success metric | ≥ 200 completed pairs | **Net P&L per first-leg fill**, including failures | Counting completed pairs ignores the failures, which is where the money is lost. |
| "No directional speculation" | stated as a property | Treat as a target | Every unmatched leg is a directional position chosen by the counterparty. Measure it (§6). |
| Statistical test | lower bound "accounting for correlation" | Block bootstrap of per-window P&L, 1-hour blocks; plus a markout test | 21 daily P&L points are too few for a meaningful bound. ~2,000 BTC windows in 21 days, resampled in blocks, are enough. |

Add these limits:

- Max 2 pair attempts per 15-minute window.
- 3 failed hedges in a row → pause 1 hour.
- Fail closed: cancel everything if the spot feed is stale > 750 ms, a Kalshi
  WS sequence gap appears, the local clock drifts > 250 ms, or vol spikes (§5.5).
- Blackout ±5 min around scheduled US macro releases (CPI, NFP, FOMC).

### Fees (verify against the current schedule before relying on these)

Kalshi fees, per contract, rounded up per order:
taker ≈ `0.07 · P · (1−P)`; maker (on series with maker fees) ≈
`0.0175 · P · (1−P)`.

| P | Taker ¢/contract | Maker ¢/contract |
|---|---|---|
| 0.30 | 1.47 | 0.37 |
| 0.40 | 1.68 | 0.42 |
| 0.50 | 1.75 | 0.44 |

The plan's example (10 YES @46 + 10 NO @48): gross 60¢; maker fees round up
to 5¢ per leg, so **net ≈ 50¢** if both legs fill. One taker exit on a
failed leg costs ~18¢ in fees alone, before any price loss.

The fee engine should use integer cents/centi-cents and implement
Kalshi's documented rounding exactly, with tests. Don't use floats.

### When does the plan break even?

Let `q` = probability the second leg fills after the first one does,
`s` = pair spread (1 − YES price − NO price), `f` = maker fees per pair,
`Lf` = average loss on a failed hedge (held to settlement or cut).

    EV per first-leg fill ≈ q · (s − f) − (1 − q) · Lf

With s = 6¢, f ≈ 1¢, Lf ≈ 8¢ (realistic once adverse selection is included):
**break-even needs q > 62%.** Paper trading must estimate `q` and `Lf` directly.
If `q` stays below about 60% with these spreads, the strategy loses money
however good the spreads look.

---

## 3. Comparison with the current strategy in this repo

| Aspect | Current bot (`avellaneda.py`) | Problem in BTC 15m | v0.2 proposal |
|---|---|---|---|
| Anchor price | Kalshi book mid | Mid trails spot; we quote on a stale price | Spot-derived fair value (§5.1) |
| Data path | REST poll every `dt = 5 s` | 5 s stale quotes = ~3.6¢ risk per 1σ move with 10 min left | Kalshi WS (`orderbook_delta`, `trade`) + exchange spot WS |
| Horizon | `T = 28800` (8 h) counted from **worker start** | `time_remaining` doesn't refer to the contract's close at all | Time to close from `close_time` |
| Volatility | constant `sigma = 0.10` | Not linked to BTC vol | EWMA of 1 s BRTI-proxy returns |
| Inventory | signed YES count; contract caps | No dollar limits, fees, or loss stops | Dollar caps, pair ledger, loss stops |
| Market choice | 20k+ markets, scored by volume and **tight** spread | Prefers markets where there is *less* to earn | Fixed series: KXBTC15M live-paper; KXETH15M observe |
| Fill model | Real orders on demo | No offline replay, no fill simulation | Conservative queue simulator (§4) |
| P&L / fees | None | Can't tell if it earns anything | Ledger with fees, markouts, failed hedges |

**Reuse:** `kalshi_api.py` signing/backoff, `fields.py`,
`cleanup.py`'s stop-then-cancel invariant, the reduce-only idea, and the
`AbstractTradingAPI` interface (a `PaperTradingAPI` can implement it, so
strategy code doesn't change between paper and any future live mode).
**Replace:** Avellaneda quoting and generic selector for this use case.

---

## 4. Paper Engine v0.2: fill simulator rules

The simulator should be pessimistic about every assumption.

1. **Latency.** Orders become live 250 ms after the decision. Cancels take
   effect 250 ms after the decision. During that window the order can still
   fill. Most adverse fills happen here, so this delay must be simulated.
2. **Queue position.** On placement, queue ahead = displayed size at our
   price at that time. Reduce it only by **trades** at our price. Never
   credit cancellations as ahead of us (assume we were last).
3. **Fill conditions.** Partial fill when trade volume at our price exceeds
   queue ahead. Full fill when a trade prints **through** our price. Touching
   our price does not count as a fill.
4. **Side mapping.** A `trade` with `taker_side = yes` at `yes_price = p`
   matches resting **NO bids at 1 − p**. A trade at p + 1¢ means NO bids at
   1 − p were used up.
5. **Stress runs.** Replay each day at 2× latency and 1.5× queue ahead. The
   strategy must be positive under the base case and not
   catastrophic under stress.
6. **Recording.** Log every WS message and every decision with sequence
   numbers, so any day can be replayed with different parameters.

---

## 5. Proposed strategy: spot-anchored pair maker

### 5.1 Fair value

- Build a BRTI proxy: median of mid prices from the exchanges in the index
  (Coinbase, Kraken, Bitstamp, Gemini, …), via their WebSockets.
- The strike is the previous window's 60 s average (known at window open).
- `FV_yes = P(avg of BRTI over final 60 s ≥ strike)` under a driftless
  random walk with EWMA-estimated vol. Include the averaging: once the final
  minute begins, part of the average is already fixed and the remaining
  variance shrinks.
- Check calibration in observation mode before trusting it: put FV in
  buckets and compare with settlement frequency. Also log Kalshi mid − FV
  over time; if Kalshi mid consistently leads FV, our spot feed is too slow.

### 5.2 Required half-spread and time-to-close

For an at-the-money digital, a spot move during our reaction delay `L`
shifts fair value by about `0.4 · √(L/τ)` per 1σ move (τ = time left).
**This doesn't depend on BTC volatility**, which makes it a good rule for
setting the time cutoff:

| Reaction delay L | τ = 14 min | 10 min | 5 min | 3 min | 90 s |
|---|---|---|---|---|---|
| 5 s (current REST bot) | 3.1¢ | 3.6¢ | 5.1¢ | 6.6¢ | 9.4¢ |
| 1 s | 1.4¢ | 1.6¢ | 2.3¢ | 3.0¢ | 4.2¢ |
| 0.3 s (WS + spot feed) | 0.8¢ | 0.9¢ | 1.3¢ | 1.6¢ | 2.3¢ |

Implications: quote mostly in the **first ~10 minutes** of a window, widen as
`1/√τ`, and stop at 3 minutes until paper data justifies trading later.

### 5.3 Quotes

    yes_bid = FV − h_yes
    no_bid  = (1 − FV) − h_no
    h = maker_fee + c · 0.4 · √(L/τ) + inventory_skew

- Constraint: `yes_bid + no_bid ≤ 1 − fees − m_min` (default m_min = 2¢).
- Join the best bid if allowed by the constraint, improve by 1¢ only if
  the constraint still holds, otherwise don't quote. **Don't quote just
  because the book is wide.**
- Size: ≤ $5 per side, ≤ 1 open pair attempt at a time.

### 5.4 Leg management (the 20-second rule)

After leg A fills at price a:

1. Cancel any remaining A-side order immediately.
2. Reprice the B bid each tick to the lower of: the quote rule above,
   and the pair break-even `1 − a − fees`. **Never pay more than
   break-even.**
3. At 20 s or τ < cutoff: stop chasing. **Hold to settlement.** Mark at the
   lower of FV and (best bid − taker fee).
4. Cross (taker exit) only when a loss stop or the unmatched cap requires it.

### 5.5 Regime filter

Don't quote when:
- 1-minute realised vol > 2.5 × its 24-hour median,
- the Kalshi book is one-sided or its top-of-book spread < 2¢ (no room after fees),
- the spot feed is stale, or exchanges in the proxy disagree by > 0.05%,
- inside the macro-release blackout.

### 5.6 Shadow taker signal (log only, no trades)

Record every time `Kalshi best ask < FV − taker_fee − 2¢` along with what it
would have earned at settlement. If the Kalshi book trails spot, a small
selective taker strategy may beat passive making. That's a candidate for v0.3
and must not be traded in v0.2.

### 5.7 ETH

Same engine and model; ETH vol from its own feed. Observation mode
(shadow quotes, simulated fills, full ledger) from day one. Promote
only after BTC passes §6 and ETH passes it on its own data.

---

## 6. How to judge it

**Early stop/go: markouts (within ~3–5 days).** For each simulated fill,
record `FV(t + Δ) − fill price` (sign-adjusted) for Δ = 1 s, 5 s, 30 s and at
settlement. If the average 30 s markout after ~500 fills is worse than −(half
spread), we are being picked off. Stop and redesign rather than wait 21 days.

**Ongoing dashboard:** completion rate `q`, Lf, net P&L per first-leg
fill, per window, per day; fees; simulated slippage; fill rate; drawdown;
markouts by τ-bucket and regime; limit hits.

**Go/no-go for real money (unchanged in spirit, tighter in form):**
- ≥ 21 days forward paper, parameters frozen before a final ≥ 7-day
  out-of-sample period;
- net P&L > 0 after fees under base assumptions, and ≥ break-even under 2×
  latency stress;
- 95% block-bootstrap lower bound (1-hour blocks over per-window P&L) > 0;
- average 30 s markout > −(average half spread);
- zero unresolved risk-control failures; every order and position accounted for;
- then live with $50, compare real fills against the simulator for 2 weeks
  before any increase.

---

## 7. v0.2 build order

1. `ws/` Kalshi WebSocket client: snapshot + deltas, seq-gap detection →
   resubscribe, heartbeats, recorder to disk.
2. `spot/` exchange feeds + BRTI proxy + EWMA vol.
3. `model/fair_value.py` with the averaging-window digital; calibration report.
4. `paper/` simulator (§4) implementing `AbstractTradingAPI`; exact fee engine.
5. `risk/` dollar limits, loss stops, fail-closed triggers, unmatched cap.
6. `strategy/pair_maker.py` (§5).
7. Ledger (SQLite) + dashboard + replay CLI.
8. Chaos test: forced disconnects every N minutes over a 24 h paper run.

The live order path stays disabled in v0.2.
