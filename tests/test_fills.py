"""Cost model calibration — reproduces Dhan's Transaction Estimator to the
paisa. If a statutory rate changes, recalibrate against a fresh estimator
screenshot and update BOTH the FeeConfig defaults and these pinned totals.
"""

from app.core.contract import Action
from app.engines import fills as F


def test_charges_match_dhan_estimator_2026_07():
    # Dhan Transaction Estimator, 2026-07-13, NSE index option, turnover 6,734:
    #   buy  26.63 = brok 20 + exch 2.39 + GST 4.03 + SEBI 0.01 + stamp 0.20
    #   sell 36.53 = brok 20 + exch 2.39 + GST 4.03 + SEBI 0.01 + STT 10.10
    cfg = F.FeeConfig()
    assert round(F.charges(6734.0, Action.BUY, cfg), 2) == 26.63
    assert round(F.charges(6734.0, Action.SELL, cfg), 2) == 36.53


def test_stt_only_on_sell_stamp_only_on_buy():
    cfg = F.FeeConfig()
    buy, sell = F.charges(10000.0, Action.BUY, cfg), F.charges(10000.0, Action.SELL, cfg)
    # sell carries 0.15% STT (15.00); buy carries 0.003% stamp (0.30)
    assert round(sell - buy, 2) == round(10000 * cfg.stt_sell_pct - 10000 * cfg.stamp_buy_pct, 2)
