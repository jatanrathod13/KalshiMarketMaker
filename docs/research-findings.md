# Research findings: BTC 15-minute up/down markets

Date: 2026-10-15. Paper research only. Scripts are in `research/`. All
results are out of sample unless stated otherwise.

## Data actually used

- **BTC/USD 1-minute candles from Bitstamp** (an exchange in the CF BRTI
  index), 2025-01-07 → 2026-10-15: 930k minutes, 62k 15-minute windows.
  Source: github.com/ff137/bitstamp-btcusd-minute-data (CC BY 4.0).
- **Settlement approximation:** KXBTC15M compares the 60 s BRTI average
  before close with the 60 s average before open. I approximate each 60 s
  average with the typical price (O+H+L+C)/4 of that minute on Bitstamp.
- **Not available from this environment:** Kalshi prices and order books,
  Coinbase/Binance data, and any ETH minute data (the network policy blocks
  those hosts). So nothing below measures Kalshi pricing directly. §5 lists
  the tool that does.

## 1. Technical indicators at the window open (Experiment 1)

Walk-forward test, refit monthly, 45k out-of-sample windows (2025-07 → 2026-10):

| Model | Hit rate (all) | Hit rate when model ≥ 55/45 | Log-loss gain vs 50/50 |
|---|---|---|---|
| Logistic, 21 TA features | 53.5% | 57.1% | 0.0032 |
| LightGBM, same features | 53.8% | 56.9% | 0.0039 |

That looks good, but most of it isn't TA:

- **The mechanical strike gap does most of the work.** The strike is the
  *average* of the last minute before open, while spot at the open is a
  single price. Their difference is known at the open. A plain random-walk
  model using only that difference already hits **53.1%** (55.7% on its
  confident subset). Any competent Kalshi market maker prices this in.
- **What TA adds beyond that is short-term mean reversion.** Features that
  measure "price stretched relative to recent levels" (1-min RSI, deviation
  from the 4-hour volume-weighted average price (VWAP), 15-min RSI, distance
  from the 24 h high, Bollinger z) carry negative weight. Trend features (EMA
  9/21, 4 h return) carry small positive weight.
- **Edge beyond the mechanical price**, if Kalshi priced exactly the
  mechanical fair value with no spread:

| Quarter | Trades | Gross edge | After taker fee (10-lot) + 1¢ spread |
|---|---|---|---|
| 2025 Q3 | 4,822 | +4.5¢ | +1.5¢ |
| 2025 Q4 | 4,838 | +3.6¢ | +0.6¢ |
| 2026 Q1 | 4,813 | +5.4¢ | +2.4¢ |
| 2026 Q2 | 5,170 | +3.3¢ | +0.3¢ |
| 2026 Q3 | 5,206 | +3.1¢ | +0.1¢ |
| 2026 Q4 (2 weeks) | 800 | +1.6¢ | −1.5¢ |

  Pooled after costs: +0.9¢ per contract, 95% block-bootstrap CI [+0.25¢,
  +1.5¢]. But the recent quarters are near zero.

**Conclusion:** taking liquidity on TA signals doesn't pay reliably after
Kalshi's taker fee, and the edge is shrinking. A gross edge of ~3¢ is useful
as a **quote skew for a maker**, who pays ~0.5¢ in fees instead of ~2–3¢.
This matches outside evidence: TurbineFi's backtests of 4,904 KXBTC15M
strategies found ~2% profitable, all 432 mean-reversion variants lost as
takers, and their one winning family ("panic fade") lost in a later rerun.

Caveat: some of the mean reversion may be Bitstamp microstructure that the
multi-exchange BRTI averages away. The recorder (§5) checks this on real
Kalshi prices.

## 2. Pricing the contract during the window (Experiment 2)

The naive random-walk digital model is **miscalibrated, by up to 12¢**:

- Minutes 0–4: overconfident in the tails (fat tails / jumps). When it
  says 1%, the true rate is ~12%.
- Minutes 6–14: **underconfident**. With 1 minute left, model 35% → actual
  23%; model 75% → actual 85%. Leaders hold their lead more often than a
  random walk predicts.

A calibrated model (per-minute monotone logistic on the model's z-score),
**fit on 2025 and tested on 2026**, improves log-loss at every minute. Its
worst probability bucket is off by **1–3.5¢** for minutes 0–12. It's now in
`kalshi_market_maker/model/fair_value.py` (coefficients in
`fv_coefficients.json`, fit on the full sample, with tests).

Sample fair values (σ = 0.07%/min ≈ $70/min at $100k):

| Spot − strike | −$150 | −$50 | −$20 | 0 | +$20 | +$50 | +$150 |
|---|---|---|---|---|---|---|---|
| Minute 0 | 0.34 | 0.44 | 0.48 | 0.50 | 0.52 | 0.56 | 0.66 |
| Minute 6 | 0.19 | 0.37 | 0.45 | 0.50 | 0.56 | 0.63 | 0.81 |
| Minute 12 | 0.05 | 0.23 | 0.38 | 0.50 | 0.62 | 0.77 | 0.95 |

If Kalshi's crowd or bots price late-window contracts closer to a naive
model, they **underprice favourites by 5–10¢ in minutes 9–14**. That's
consistent with the favourite–longshot bias documented across Kalshi
(Becker: takers −1.1% / makers +1.1% per trade; Bürgi, Deng & Whelan:
longshots lose most). It's the strongest *candidate* edge found here, but
it's unverified until compared with recorded Kalshi prices.

## 3. What the evidence says about the strategy

| Idea | Evidence | Status |
|---|---|---|
| Directional TA as taker | +0.9¢ net pooled, ~0 recently | **Rejected** for live; use as quote skew only |
| Naive two-sided MM at book mid | Stale quotes; anchored to the wrong price | **Rejected** |
| Maker quotes around calibrated FV, skewed by TA residual | Makers earn on Kalshi as a class; our FV is 1–3.5¢ calibrated OOS | **Lead strategy**, pending §5 |
| Late-window favourite buying, as maker | Naive-model underconfidence 5–10¢; longshot bias literature | **Candidate**, pending §5 |
| ETH lagging BTC moves | Vendor studies mixed; one lost on all 100 variants | Observe only |
| Taking stale Kalshi asks vs FV | Unknown until §5 | Observe only |

## 4. The strategy to run in paper (v0.2)

**FV-anchored, signal-skewed passive maker.**

1. `fair = FairValueModel.p_yes(spot, strike, σ, minutes_elapsed)`, with spot
   from a multi-exchange median and σ from `EwmaVol` on 1-minute closes.
2. In minutes 0–5, shift fair value by half of the open-time TA residual
   (logistic(FV + TA) − FV), capped at ±3¢. Halving it keeps a decaying
   signal from turning into directional betting.
3. Rest YES bid at `fair − h` and NO bid at `(1 − fair) − h`. Set
   `h = max(2¢, maker fee + 0.4·√(L/τ))` (L = reaction delay, τ = time
   left; see strategy-v2 §5.2). Never cross the book. Cancel when fair
   value moves ≥ 1¢ or the spot feed is stale.
4. Quote minutes 1–12 only. $5 per side, $5 total unmatched exposure, other
   limits per strategy-v2 §2.
5. After one leg fills, raise the other leg's bid toward pair break-even.
   Otherwise hold to settlement (strategy-v2 §5.4).

## 5. The deciding test

`research/recorder.py` logs Kalshi books, trades and settlements for
KXBTC15M and KXETH15M, alongside Coinbase spot, using public endpoints
only. `research/evaluate_recording.py` then reports:

- **A.** Kalshi mid − our FV by minute, plus the average spread.
- **B.** Brier score, Kalshi mid vs our FV. **If Kalshi beats us at every
  minute, stop.** The crowd knows more than this model, and quoting around it
  would be picked off.
- **C.** Taker P&L when ask + fee + margin < FV.
- **D.** Maker P&L per fill at FV − h, with pessimistic fills (print strictly
  through our price) and optimistic fills (print at our price).

Go to the full v0.2 paper engine only if **B** shows our FV at least as
accurate as Kalshi in some minute range, and **D (pessimistic)** is positive
there with a 95% interval above zero across ≥ 3 days.

Run it on any machine that can reach `api.elections.kalshi.com` and
`api.exchange.coinbase.com`:

    pip install requests
    python research/recorder.py --out data/recordings      # leave it running 3–7 days
    python research/evaluate_recording.py data/recordings/*.jsonl

Or add those two hosts to this cloud environment's allowed domains and
it can run here.

## 6. Expectations

At $250, even a real 1¢ edge per contract on 10-contract clips and ~30
fills a day is about **$3/day**. Kalshi book depth limits size, and every
edge here is small and decays as others find it. This can become a
disciplined, positive-expectancy side project. It isn't a reliable income
source. Don't put in money you need.

## Sources

- Bitstamp BTC/USD minute data: github.com/ff137/bitstamp-btcusd-minute-data
- J. Becker, "The Microstructure of Wealth Transfer in Prediction Markets":
  jbecker.dev/research/prediction-market-microstructure
- Bürgi, Deng & Whelan, "Makers and Takers: The Economics of the Kalshi
  Prediction Market": karlwhelan.com/Papers/Kalshi.pdf
- TurbineFi, "Kalshi 15-Minute Bitcoin Strategies: 4,904 Backtests":
  turbinefi.com/blog/5000-strategy-backtest-kalshi-btc-15m
- KXBTC15M settlement terms (CF Benchmarks RTI, 60 s averages):
  defirate.com/prediction-markets/crypto/sources/
