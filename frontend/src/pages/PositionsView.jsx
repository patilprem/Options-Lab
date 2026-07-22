import { useEffect, useState } from 'react'

const fmt = n => '₹' + (n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })
const fmt2 = n => '₹' + (n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const pnlCls = n => n >= 0 ? 'pos' : 'neg'
const sign = n => (n >= 0 ? '+' : '') + fmt2(n)
const SCANNER_ID = 'SCANNER'

export default function PositionsView({ onStrategyClick }) {
  const [pf, setPf] = useState(null)

  useEffect(() => {
    const load = async () => {
      try {
        const data = await fetch('/portfolio/today').then(r => r.json())
        setPf(data)
      } catch (e) {
        console.error(e)
      }
    }
    load()
    const iv = setInterval(load, 2000)   // live dashboard — keep it fresh
    return () => clearInterval(iv)
  }, [])

  if (!pf) return <div className="panel-body"><div className="empty">Loading...</div></div>

  const t = pf.totals
  const openRows = pf.open_positions || []
  const closedRows = [...(pf.closed_positions_today || [])].reverse()
  // the scanner auto-trader isn't a Strategy record, so it has no detail
  // page to drill into — only strategy-sourced rows are clickable
  const drillable = row => row.strategy_id && row.strategy_id !== SCANNER_ID

  return (
    <div className="panel-body">
      <div style={{ display: 'flex', gap: '8px', marginBottom: '14px' }}>
        <span className="badge VALIDATED" style={{ padding: '6px 14px', fontSize: '12px' }}>PAPER · active</span>
        <span className="badge STOPPED" style={{ padding: '6px 14px', fontSize: '12px' }}>LIVE · not enabled</span>
      </div>
      <div className="metrics">
        <div className="metric">
          <div className="k">Today's P&L</div>
          <div className={`v ${pnlCls(t.day_pnl)}`}>{sign(t.day_pnl)}</div>
          <div className="sub">{pf.date} · paper</div>
        </div>
        <div className="metric">
          <div className="k">ROI on capital</div>
          <div className={`v ${pnlCls(t.day_roi_pct)}`}>{t.day_roi_pct}%</div>
          <div className="sub">on {fmt(t.allocated_capital)} allocated</div>
        </div>
        <div className="metric">
          <div className="k">ROI on margin</div>
          <div className={`v ${pnlCls(t.day_roi_on_margin_pct)}`}>{t.day_roi_on_margin_pct}%</div>
          <div className="sub">on {fmt(t.margin_used)} deployed</div>
        </div>
        <div className="metric">
          <div className="k">Open / closed</div>
          <div className="v">{t.open_positions} / {closedRows.length}</div>
          <div className="sub">positions / closed today — strategies + scanner</div>
        </div>
      </div>

      <h3 className="sec">Open positions</h3>
      {openRows.length ? (
        <div className="table-scroll"><table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Contract</th>
              <th>Qty</th>
              <th>Entry</th>
              <th>MTM</th>
              <th>Unrealized</th>
              <th>Stop</th>
            </tr>
          </thead>
          <tbody>
            {openRows.map((p, i) => (
              <tr key={`${p.strategy_id}-${i}`}
                  style={drillable(p) ? { cursor: 'pointer' } : undefined}
                  onClick={() => drillable(p) && onStrategyClick(p.strategy_id)}>
                <td style={{ fontFamily: 'var(--body)', fontWeight: '600' }}>{p.strategy}</td>
                <td style={{ textAlign: 'left' }}>
                  {p.symbol ? `${p.symbol} ` : ''}{p.strike ? `${p.strike} ` : ''}{p.type}{p.tag ? ` · ${p.tag}` : ''}
                </td>
                <td>{p.qty}</td>
                <td>{fmt2(p.entry)}</td>
                <td>{fmt2(p.mtm)}</td>
                <td className={pnlCls(p.unrealized)}>{sign(p.unrealized)}</td>
                <td>{p.stop_loss != null ? fmt2(p.stop_loss) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      ) : (
        <div className="empty">No open positions right now.</div>
      )}

      <h3 className="sec">Closed positions (today)</h3>
      {closedRows.length ? (
        <div className="table-scroll"><table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Contract</th>
              <th>Qty</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>P&amp;L</th>
              <th>Held</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {closedRows.map((c, i) => (
              <tr key={`${c.strategy_id}-${c.exit_ts}-${i}`}
                  style={drillable(c) ? { cursor: 'pointer' } : undefined}
                  onClick={() => drillable(c) && onStrategyClick(c.strategy_id)}>
                <td style={{ fontFamily: 'var(--body)', fontWeight: '600' }}>{c.strategy}</td>
                <td style={{ textAlign: 'left' }}>
                  {c.symbol ? `${c.symbol} ` : ''}{c.strike ? `${c.strike} ` : ''}{c.side || ''}
                </td>
                <td>{c.qty}</td>
                <td>{fmt2(c.entry_price)}</td>
                <td>{fmt2(c.exit_price)}</td>
                <td className={pnlCls(c.pnl)}>{sign(c.pnl)}</td>
                <td>{c.held_minutes != null ? `${c.held_minutes}m` : '—'}</td>
                <td style={{ textAlign: 'left' }}>{c.reason || ''}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      ) : (
        <div className="empty">
          Nothing closed yet today — once a position exits, it moves here
          with its entry/exit price and realized P&amp;L.
        </div>
      )}
    </div>
  )
}
