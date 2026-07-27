# Scanner-Trader Chart Analysis (daily)

Recurring exercise: each day, review 5m option-premium charts for every
contract the scanner-trader touched, mark up good entry / good exit / where
a stop-loss is hard to hit (i.e. genuine trend continuation vs. noise), then
compare that read against the actual `scanner_journal` round trips for the
day. Purpose: find *entry timing* and *exit timing* biases in the live
scoring logic (`entry_score`/`exit_score`/`hard_stop_pct`/`trail_pct`/
`target_pct` — see `app/engines/adaptation.py`), not to hand-tune P&L after
the fact.

How the data for each entry is pulled: `docs/chart_analysis.md` methodology
note — SSH to the VPS, dump `scanner_journal` rows for the day (see chat
history for the exact `sqlite3`/python3 one-liners), pull the day's option
charts from the broker app, then do the write-up below.

**2026-07-27 — the 07-23..26 entry-distance data is NOT USABLE.** It carried a
systematic selection bias: a symbol was only sampled once held or at the
instant of entry, so a FIRST entry always had one sample (VWAP == the entry
price, recorded as a real-looking `0.00`) and only RE-entries carried history.
In the 07-24 rows that split perfectly — every `0.00` was a first entry, every
populated row a re-entry, and ~70% of the populated rows were INFY churn (the
day's worst losses). Bucketing that would have "proven" that entries below
VWAP lose money, when it was really measuring churn. Fixed 2026-07-27:
`_sample_candidates()` samples EVERY ranked candidate each cycle (cache-only,
no extra API calls), `opt_prem_samples` records the window size so thin rows
are filterable, thin windows now report `null` instead of `0.00`, and the
ambiguous `opt_dist_to_lower_bb_pct` was replaced by `opt_pct_b` (position
within the band, 0=lower/1=upper — comparable across names and volatility,
which distance-to-lower-band was not). Analysis starts from 2026-07-28 data.

**2026-07-23+:** `entry_ctx` now also logs `opt_dist_to_vwap_pct` and
`opt_dist_to_lower_bb_pct` — the option premium's own distance from its
session VWAP / lower Bollinger band at entry, computed from an in-memory
rolling window of the scanner's own premium samples (`ScannerTrader._prem_hist`
in `app/engines/scanner_trader.py`). Same field for CE and PE: the strategy
only ever buys premium, so "cheap relative to its own session" is the same
test either way. Both are `null` until that (symbol, side) has built up
enough same-day samples (Bollinger needs ≥5). This turns the "chased an
already-extended move" read from earlier days from a chart-eyeball into a
number — check whether entries with a strongly negative `opt_dist_to_vwap_pct`
/ `opt_dist_to_lower_bb_pct` (bought below their own recent average) win more
than entries near/above it, across enough days to clear the ≥3-distinct-day
persistence bar before treating it as a real signal.

---

## 2026-07-22

### Day summary

25 closed round trips, 3 still open at 15:35 IST (DRREDDY 1170 PE, CIPLA
1410 PE, INDIGO 5150 PE). **Win rate 20% (5/25)**. **Net realized (closed
only): ‑₹10,049.** Every single closed trade exited with `reason:
"setup_gone"` — **not one trade hit `hard_stop_pct` (30%) or `target_pct`
(100%)**. The score-decay exit (`exit_score` < 45) is the *only* thing
closing trades today; the configured hard stop/target never engaged because
they're set far too wide (30%/100%) to ever be the binding constraint at
these hold times (avg ~28 min, median ~14 min).

| Symbol | Trades | Wins | Net realized |
|---|---|---|---|
| TVSMOTOR | 6 | 1 | -2,408.99 |
| NESTLEIND | 5 | 2 | -1,817.73 |
| DRREDDY | 4 | 1 | -1,725.32 |
| GODREJPROP | 4 | 1 | -1,242.36 |
| BAJAJ-AUTO | 3 | 0 | -1,105.74 |
| LODHA | 2 | 0 | -823.44 |
| PERSISTENT | 1 | 0 | -925.09 |
| CIPLA | 1 (open) | — | unrealized |
| INDIGO | 1 (open) | — | unrealized |

### Cross-cutting pattern (all 9 charts)

The single biggest recurring failure mode: **entries are taken right after
an already-extended move ("pressing the day's high/low", high `volume_surge`,
big `price_change_pct`), i.e. near the local top/bottom of a swing, not at
the start of one.** The scoring model (`entry_score` from momentum + OI +
volume) fires *because* a move has already happened, which is naturally
close to a short-term exhaustion point. What follows in most charts is a
1–5 point pullback/consolidation — exactly enough to decay `exit_score`
below 45 and trigger `setup_gone` for a small-to-moderate loss, just before
price often resumes in the original direction without the position.

Second pattern: when a trade **does** run long (held_minutes > 60), MFE is
usually given back almost entirely by exit time (DRREDDY 170-min hold: MFE
+12.33% → exit ‑6.61%; PERSISTENT 95-min hold: MAE ‑16.3% → exit only
‑5.59%, i.e. noisy round trip either way). There is no trailing lock-in of
open profit — `trail_pct` (0.25) doesn't appear to be engaging before
`setup_gone` does.

Third: the **5 winners** share a distinct signature — entered on a fresh
bounce off a just-flattened base/low (TVSMOTOR 12:35 entry @48.70, held
only 15 min, +17.66%; NESTLEIND 11:44 entry @26.85, held 7 min, +5.4%) or
caught a continuation leg early rather than late (DRREDDY 09:56 entry
@21.05, held 28 min, +13.54%). None of the winners were "moved X%, pressing
the day's high/low" chase entries at a fresh multi-candle extreme — they
were entries into a *pause*, not a spike.

### Per-contract chart read vs. actual trades

**DRREDDY JUL PE** (1200→1190→1170 strikes traded intraday) — Chart: violent
spike 09:15–10:00, then a wide 28–36 chop band 10:00–14:00, then a blow-off
spike to ~42 at 14:00–14:15 (marked by the chart's own sell arrows) fading
back to ~35 by close.
- Good entry: on the initial breakout confirmation, like the 09:56 entry
  (+13.54%, the day's best DRREDDY trade) — buying a fresh leg, not a
  spike top.
- Good exit: right at/near the 14:00 spike peak (~40–42).
- Hard-to-hit-SL zone: the 10:00–14:00 chop band — many small oscillations
  inside a tight range where a normal SL would whipsaw repeatedly; the
  09:21 (‑9.19%) and 10:31 (‑6.47%) entries were both taken chasing a
  move that had *just* extended, immediately followed by exactly this kind
  of chop.
- Actual trades: 1 big win (09:56), 3 losses, including the 170-min hold
  that rode the whole chop band, caught the 14:00 spike (MFE +12.33%) and
  still gave it back to ‑6.61% by the time `setup_gone` fired.

**BAJAJ-AUTO JUL CE** (10700/10800/11000 strikes) — Chart: vertical spike
09:15–09:45, chop into 10:00, then a long clean grind up 10:00–14:00,
accelerating 14:00–14:30 to the day's high (option +475% for the full
session) before a pullback into the close.
- Good entry: any dip during the 10:00–14:00 grind, or the very start of
  the 14:00 acceleration.
- Good exit: into the 14:00–14:30 spike, near the day high.
- Hard-to-hit-SL zone: the 10:00–14:00 grind itself — shallow pullbacks,
  hard to get stopped if entered on a dip rather than a spike.
- Actual trades: all 3 (09:42, 10:31, 13:05) were late/chase entries
  ("pressing the day's high") right before a pause — 2 losses + 1
  breakeven. This was the best-trending name of the day and the strategy
  captured almost none of it.

**INDIGO JUL 5150 PE** — Chart: steady one-way grind up all session
(option +116%, 65→150), consistently holding above its short MAs, no real
counter-trend — the cleanest trend day of the set.
- Good entry: 09:56, right where it was actually taken.
- Good exit: none needed yet — still open.
- Hard-to-hit-SL zone: effectively the whole day; price never gave back
  more than a shallow pullback.
- Actual trade: entered once (09:56 @104), never re-entered/never
  `setup_gone`'d out — the single best-handled name today, precisely
  *because* the strategy didn't chase or get shaken out.

**TVSMOTOR JUL CE** (3920/3940 strikes) — Chart: early spike to ~74 by
09:30, then a grinding decline the rest of the day to ~34–40 by close, with
a brief bull-trap bounce near 13:00 (marked by chart arrows) that rolled
over again.
- Good entry: the 12:35 basing/bounce off the flattened low (~48.70) —
  exactly the trade that returned +17.66% in 15 minutes.
- Good exit: fast, within the same bounce, before the decline resumed.
- Hard-to-hit-SL zone: none really — this was a fading name after 10:00,
  so CE re-entries chasing bounces were structurally against the trend.
- Actual trades: 6 total, only the 12:35 bounce entry won; the 10:17 entry
  (bought the local high @63.50) reversed almost immediately for ‑15.04%,
  and the 13:05 entry (chased the marked local top) saw a 17.48% MAE
  before a ‑6.12% exit. Classic "buy the spike, get faded" set.

**LODHA JUL 1160 PE** — Chart: early spike, chop, a second push down
around 12:00–12:15 (chart's arrows), then range-bound 12:15–15:00 (20–24
band), closing ~21–22 — essentially sideways after the morning move.
- Good entry: none obviously good; this was a chop day.
- Hard-to-hit-SL zone: the entire 12:15–15:00 range — low-conviction chop.
- Actual trades: 3, all repeat "pressing the day's low" short_buildup
  chases into a name that just wasn't trending — 2 small-to-moderate
  losses (‑24, ‑799) and no wins.

**CIPLA JUL 1410 PE** — Chart: falling from ~33 to ~25.4 between 13:15–14:00
(good for a held PE), then a small recovery into the close (~26.85).
- Actual trade: entered once (10:45 @28.35), still open; the leg lower that
  would have most benefited the position happened *after* entry but the
  premium then recovered into the close, leaving the open position only
  modestly underwater (~28.35 → ~26.85) despite the underlying's overall
  favorable move for the day.

**NESTLEIND JUL CE** (1480/1490/1500 strikes) — Chart: one violent spike
09:20–09:30 (5→39, the single largest volume bar of the whole set)
immediately faded hard (chart's arrows) back to ~28–30 by 10:00, then a
long, tight 17–25 chop/mild-uptrend the rest of the day, closing ~22.
- Good entry: a bounce off the post-spike low, like 11:44 (+5.4%, 7-min
  hold) or 10:53 (+5.71%, 21-min hold) — both entries into a *pause*, not
  the spike itself.
- Bad entry pattern: chasing the loudest volume print — the 14:13 entry
  (9x volume, "pressing the day's high") was this day's single largest
  loss (‑1,525.74, ‑10.96%, MAE ‑13.04%) — bought exhaustion volume right
  before the fade.
- Actual trades: 5, 2 wins / 3 losses — a fairly literal illustration of
  "biggest volume spike ≠ best entry."

**PERSISTENT JUL 5100 PE** — Chart: initial spike, then a long grinding
decline most of the day (~130→~110–113), with the tail-end decline
(chart's arrows) around 14:45–15:00.
- Actual trade: 1, entered 11:22 into a genuine downtrend (correct
  direction), but MAE hit ‑16.32% intrabar (noisy chop) before `setup_gone`
  closed it for only ‑5.59% — direction was right, timing/size of the
  adverse excursion was the cost, not a stop-loss (never got near the 30%
  hard stop).

**GODREJPROP JUL 2080 PE** — Chart: the cleanest, strongest one-way uptrend
in the whole option premium (12→39, +163%, a near-straight diagonal channel
all session) — yet this name had **4 trades, 3 losses, only 1 small win**
(net ‑1,242.36), the largest contradiction between chart quality and trade
outcome today.
- Why: the option premium moves in a saw-tooth, not a straight line — each
  leg up is followed by a 1–4 point pullback. All 4 entries (13:28, 13:57,
  14:21, 15:20) were taken right at the top of one of those legs (repeated
  "pressing the day's low" in the underlying), and each got `setup_gone`-
  exited during the following pullback — then the *next* leg up resumed
  without the position on. This is the single clearest example of
  "chasing an already-extended move" costing real, repeated money in an
  otherwise-perfect trend.
- Good entry would have been any of the pullback lows themselves (the
  troughs between legs), not the point where the reasons/score peaked.
- Hard-to-hit-SL zone: none — every pullback was shallow (1–4 points, well
  inside the 30% hard stop) and mean-reverted quickly; the real leak was
  the *scoring exit* firing during normal pullback noise, not a stop-loss
  at all.

### Takeaways to consider (not applied — proposals only, per adaptation.py discipline)

1. **Entry timing**: the `entry_score` formula rewards moves that have
   *already happened* (`price_change_pct`, `volume_surge`, OI change all
   look backward). Nearly every loser today was a chase into an extended
   move; nearly every winner was a bounce off a pause. Worth checking
   whether `range_pos` (day-range position) or a short pullback/basing
   filter could avoid buying local extremes.
2. **Exit timing**: zero trades hit `hard_stop_pct` or `target_pct` today —
   the wide 30%/100% config makes them decorative; `exit_score` < 45 is
   doing 100% of the risk control. That's arguably fine, but the DRREDDY
   170-min and PERSISTENT 95-min cases show it doesn't lock in favorable
   excursions (MFE 12.33% → exit ‑6.61%; nothing resembling a trailing
   stop kicked in despite `trail_pct: 0.25` in config).
3. **GODREJPROP-style saw-tooth trends**: a trend can be net +163% for the
   day and still lose money on 4 trades if every entry chases a leg-top.
   This is the most actionable single case to dig into on the actual
   `entry_score`/`buildup` classification logic.

---

## 2026-07-24

### Day summary

20 closed round trips (no positions left open at the last logged cycle,
15:35). **Win rate 20% (4/20)** — same win rate as 2026-07-22, but a much
worse day in ₹ terms: **net realized ‑₹35,008.73** vs. ‑₹10,049 two sessions
ago. The exit-reason mix flipped from last time: 2026-07-22 was 100%
`setup_gone` (score decay, zero stop-loss hits); today **10 of 20 exits were
`trail_stop` and 1 was `hard_stop`** — the stop-loss machinery was actually
doing real work today, which lines up with today being a visibly more
volatile, faster-whipsaw session across every chart reviewed (bigger spikes,
faster reversals) than the grindier 07-22 session.

| Symbol | Trades | Wins | Net realized |
|---|---|---|---|
| INFY | 9 | 2 | -15,326.92 |
| TATAELXSI | 2 | 0 | -8,680.85 |
| CIPLA | 3 | 1 | -5,241.29 |
| PERSISTENT | 3 | 1 | -5,098.82 |
| TATACONSUM | 1 | 0 | -1,014.68 |
| COFORGE | 1 | 0 | -128.93 |
| M&M | 1 | 1 | +482.76 |

(CIPLA's first exit today, ‑₹4,360.00 at 09:16, was actually closing a
position **carried over from 2026-07-22** — entered 10:45 that day, held
2,791 minutes / ~2 days, hit `trail_stop`. Confirms the earlier "does the
scanner carry positions overnight" answer in practice: yes, and this one
gave back most of its `mfe_pct: 52.56%` peak before finally stopping out.)

### Cross-cutting pattern

**INFY was churned hard and lost on almost every attempt**: 9 separate
entries into the same PE bias theme through the day (1030→1020→1020→1020→
1020→1030→1025→1025→1035, each a fresh entry after the prior one's exit),
net ‑₹15,327, only 2 winners. This is exactly the "re-entries within 30 min
of an exit are losing" pattern the app's own journal-insights engine already
flagged (visible in the Scanner page's Trade journal box: "14 quick
re-entries, net ‑₹3,810.36" — a rolling, all-time stat, but today's INFY
sequence is a clean live example of the same failure mode). The underlying
kept giving fresh `long_unwinding`/`moved -2%+` signals as it ground lower
most of the day, and the scanner kept re-buying each fresh signal — but each
new PE entry was itself already-extended (pressing the day's low again),
so most got stopped out on the next small bounce.

**Chasing a spike's peak was catastrophic today, not just costly.**
Where 2026-07-22's worst chase-losses were single-digit percent, today's
were 25–33%, because today's spikes were sharper and reversed faster:
- **TATAELXSI 3500 CE**: entered 13:52:17 @62.70 — the chart shows that
  price was the exact top tick of a huge +323%-on-the-day move, right before
  a plateau/chop. Exited 13:53:06 @44.45, **held 0 minutes**, ‑29.11%
  (`trail_stop`). A near-instant stop-out from buying the literal top.
- **TATAELXSI 3550 CE**: entered 15:16:21 @47.60 — this one bought DURING
  the final breakdown leg (the chart's last 30 minutes show a sharp decline
  off the day's high, marked by its own down-arrows), not even a top, an
  already-falling knife. Exited 6 min later @31.70, ‑33.4% (`hard_stop`) —
  today's only hard-stop hit, and the single worst trade of the day.
- **PERSISTENT 5200 CE (14:40 entry)**: entered @71.95, within a few
  candles of the chart's post-spike high (~76) before the pullback. Exited
  15 min later @52.55, ‑26.96% (`trail_stop`).

**The one clean win-after-chase-loss pattern, again**: PERSISTENT was
traded 3 times and shows the same lesson as GODREJPROP on 07-22 — buy the
pullback, not the spike, not the bounce-off-the-bounce:
- 14:40 entry @71.95 (chasing the spike high) → ‑2,488.85
- 14:58 entry @53.30, right after that pullback bottomed → **+1,530.64**
  (the win — bought the dip, not the spike)
- 15:16 entry @65.35, chasing the bounce back up again → ‑4,140.61 (biggest
  loss of the day for this name — chased the SAME bounce a second time,
  right back into another local top)

**CIPLA (1420/1430 CE)** told the same story on a smaller scale: the chart
shows CIPLA grinding down most of the day (a bad day to be buying CE at
all) with one brief bounce 11:41–11:53 that the scanner happened to catch
for +2,765.92 (its biggest winner of the day) — bracketed by a loss just
before (11:23 entry, chased a high before the bounce) and would have been a
loser again had it stayed in past 11:53 (the decline resumed immediately
after).

**COFORGE**: only 1 trade, entered 15:22:43 @30.50 — the chart shows this
was well after COFORGE's real move (a breakout from ~19 to ~32 between
13:45–14:30); the entry came on the tail end of an already-extended
breakout. Exited in 6 minutes for a near-breakeven ‑128.93 (`setup_gone`)
— the mildest version of today's "chase" pattern, caught early by the
score-decay exit before real damage.

### Early check on `opt_dist_to_vwap_pct` / `opt_dist_to_lower_bb_pct`

First day with this instrumentation warm enough to compare (yesterday it
was still cold-starting for most names). Inconclusive so far — exactly the
small-N caveat flagged when this was built:
- Both winning PERSISTENT/CIPLA bounce-trades were bought well BELOW their
  own session VWAP (‑13.77%, ‑17.74%) — consistent with "buy the pullback."
- But INFY's one winning entry (11:11, +340.57) was bought ABOVE its VWAP
  (+5.59%), and several INFY LOSSES were also bought well below VWAP
  (‑13.24%, ‑17.79%, ‑13.21%, ‑13.81%) — so being below VWAP alone did not
  save INFY's repeated re-entries from the churn problem above.
- Two days in, not close to the ≥3-distinct-day persistence bar. Keep
  logging and re-check after a few more sessions — the re-entry/churn
  problem looks like the dominant, more clearly-actionable signal so far,
  separate from the VWAP-distance question.

### Takeaways to consider (proposals only, unchanged discipline — nothing applied)

1. **A re-entry cooldown per symbol** looks like the single most actionable
   change surfaced by two days of data now — the app's own journal-insights
   already suggested this ("14 quick re-entries, net ‑₹3,810.36"), and
   today's INFY sequence (9 entries, 2 wins, ‑₹15,327) is a live example.
   Worth checking what a short cooldown (e.g. skip re-entering a symbol
   within N minutes of its own exit) would have done to today's INFY P&L.
2. **Trail-stop distance on fast spikes**: today's three worst losses
   (TATAELXSI ×2, PERSISTENT) were all "buy near a spike's peak, stop out
   within 6–15 minutes" — the trailing stop DID fire (unlike 07-22 where it
   never engaged), but only after giving back 25–33% first, because the
   stop trails the HIGH-WATER mark, not the entry, and these spikes moved
   fast enough that the high-water mark was set right near the entry price
   itself. Not obviously fixable without a tighter initial stop specifically
   for high-volatility/fast-spike entries.

---

## NIFTY 50 index — 3-day technical read (2026-07-22 to 07-24)

Standalone read of the index itself (5m), not a scanner-traded instrument —
context for *why* the stock-level entries above looked the way they did,
kept separate from the per-day trade comparisons since there's no
scanner_journal round trip to check it against.

Indicators visible on the charts: standard floor-trader **pivot points**
(the thick dotted red/yellow horizontal lines — S1/S2/PP/R1/R2), a fast and
slow **moving average pair** (orange = faster, pink/magenta = slower), a
wide-swinging **volatility band** (blue), and what reads as a
**Supertrend-style trailing stop** (the green stepped line — flat until it
flips, then trails the trend).

**2026-07-22 (Wed)**: O 24,150.45 · H 24,166.30 · L 24,076.15 · C 24,094.10
(‑98.95, ‑0.41%). Fast opening decline (first 30–45 min, ~24,150→24,000),
then a tight chop 24,000–24,040 through midday, then a second, cleaner leg
down into the close (24,000→23,917).
- Good entry (short bias): the retest of the opening-drive breakdown level
  after the first fast decline — not the first red candle itself.
- Hard-to-hit-SL zone: the midday 24,000–24,040 chop — a tight short
  entered right at the open would have been stopped/whipsawed here before
  the real afternoon continuation.
- Best exit zone: into the acceleration going into the close, the fastest,
  cleanest part of the move.
- **Near the indicators**: the opening decline sliced straight through a
  cluster of pivot levels (24,120→24,080→24,040→24,000) with barely a
  pause at any of them — strong momentum blowing through the pivot grid,
  not a level-by-level fight. The midday chop was price oscillating
  *between* the orange and pink MAs as they converged (classic
  MA-compression = indecision), while the blue volatility band contracted
  in step. The green trailing-stop line sat flat/elevated through the
  whole chop and only flipped down as the final afternoon leg broke —
  a lagging but clean trend-confirmation, not a leading signal. Close
  landed just below the lowest pivot support (~23,920) — a genuinely
  bearish close relative to the pivot grid, not just relative to the open.

**2026-07-23 (Thu)**: O 23,904.80 · H 23,916.05 · L 23,876.20 · C 23,915.35
(‑75.70, ‑0.32%). Chop 23,920–23,960 through the morning, a rounded top,
then the cleanest single trending segment of the 3 days — a sustained
breakdown from ~23,920 to a low near 23,800 through the afternoon — followed
by a sharp V-shaped short-covering bounce in the final 30–45 minutes that
round-tripped almost the entire afternoon decline back up to close near
flat (23,915, barely below the morning range).
- Good entry (short bias): the clean break of the 23,920 morning-range
  support, the best-trending window of the whole set.
- Hard-to-hit-SL zone: same morning chop pattern as 07-22.
- **The critical risk this day**: holding a short into the last 30–45
  minutes. The late reversal gave back the entire afternoon's move (~115
  points) in a short covering rally — a short had to be closed into
  strength (near the low), not held into the close.
- **Near the indicators**: the morning chop was a genuine pivot range —
  price respected both the ~23,920 support and ~23,960 resistance pivots
  repeatedly for over two hours, real S/R here, not noise. The afternoon
  breakdown was a clean break of the 23,920 pivot (not just an MA cross),
  continuing to the next pivot support near 23,840 and on to the ~23,800
  low where the reversal began. The green trailing-stop line flipped right
  at the breakdown and stayed low through the whole decline without
  re-flipping — it avoided whipsawing during the earlier chop by staying
  flat, then tracked the real move once it started. The closing V-bounce
  punched back up through the orange MA, then the pink MA, and closed
  right back **at** the same ~23,920 pivot that had been support all
  morning — a "reclaim" close, not just a bounce that stalled short of it.

**2026-07-24 (Fri)**: O 23,666.35 · H 23,752.80 · L 23,646.90 · C 23,751.70
(current as of this read; index gapped down ~249 points / ~‑1.0% overnight
from 07-23's 23,915 close). Sharp continuation of the gap-down for the
first 15–20 minutes to the day's low (~23,647), then a strong, sustained
V-shaped recovery rally through midday to ~23,800–23,820, then a choppy
23,750–23,800 consolidation into the early afternoon.
- Good entry (long bias): the exact capitulation low right as the
  gap-down's panic selling exhausted (~23,647–23,660) — buying the
  low, not the initial panic candle, led into the cleanest, longest
  trending move of the 3 days.
- Hard-to-hit-SL zone: the early-afternoon 23,750–23,800 chop — a tight
  stop on a long initiated during the rally gets tested repeatedly here
  even though the broader recovery from the gap-down low stayed intact.
- **Near the indicators**: the day's low (23,646.90) landed almost exactly
  on a pivot support line near 23,600–23,610 — the level held on its first
  and only test, a textbook pivot-support bounce, not a random low. The
  recovery climbed the pivot ladder in order — reclaiming the ~23,720
  pivot, then the ~23,800 pivot, each acting as fresh resistance that got
  tested and cleared in turn rather than one clean vertical move. The
  green trailing-stop line flipped up right as price crossed the orange MA
  during the recovery, then trailed cleanly below price the rest of the
  way — it stayed valid (no whipsaw) straight through the choppy
  23,750–23,800 consolidation, meaning that chop never actually threatened
  the uptrend by this indicator's own read, even though it would have
  stopped out a tight fixed-distance stop.

**Why this matters for the stock-level read above**: 07-24's dominant
entry reason across COFORGE, PERSISTENT, TATAELXSI, and CIPLA was
`short_covering` / `moved +X%` — i.e. a broad bullish move. NIFTY's own
gap-down-then-V-recovery this same day is very likely the macro driver:
a broad, index-wide relief rally off a capitulation low was lifting
individual FNO names with it, not name-specific news. Worth remembering
when reading any single day's stock-level "buy the pullback, not the
spike" lesson — on a day like 07-24, the whole market's "pullback" was the
index's own gap-down low, and names chasing the LATER stage of that
market-wide bounce (as several 07-24 entries did — TATAELXSI, PERSISTENT's
first two entries) were effectively chasing the SAME extended move NIFTY
itself had already made most of by midday.

*(Next day's entry: repeat the same SSH → sqlite3/python3 dump → chart
comparison, append below this line.)*
