# Market observations log

Daily post-session analysis of the live-recorded chain footprint
(`GET /data/footprint?underlying=NIFTY&date=YYYY-MM-DD`). Goal: identify
repeatable OI/IV patterns that explain intraday moves, then promote confirmed
patterns into strategy filters (the 52% → 60% path for PBK-style entries).

## Method (per session)
1. Spot narrative: open/close/range, the day's largest 5-min moves.
2. For each large move: what did OI do at the strikes around it — writers
   ADDING against the move (resistance/support being built) or COVERING
   (fuel)? Did the chain lead or follow?
3. IV: open→close per side; crush/expansion vs the move direction.
4. PCR timeline: trend + inflections vs spot inflections.
5. PBK check: did it trade? Was the chain aligned or opposed at entry?
6. One-line hypothesis per pattern; count ✓/✗ across days below.

## Pattern scoreboard (hypotheses → evidence)
| # | Hypothesis | ✓ | ✗ | Status |
|---|------------|---|---|--------|
| 1 | Sustained spot move is preceded/confirmed by writer COVERING on the pressured side (falling OI in the direction of the move) | | | watching |
| 2 | Moves into heavy fresh OI walls stall/reverse (OI added against the move) | | | watching |
| 3 | Rising PCR + spot holding lows = long bias for the afternoon | | | watching |
| 4 | IV expansion during a rally = trend day; IV crush during a rally = squeeze that fades | | | watching |
| 5 | PBK entries with chain alignment (patterns 1/3 agree) outperform unaligned ones | | | watching |

## Sessions

<!-- append one section per trading day -->
