import { useEffect, useState } from 'react'

const fmt = n => '₹' + (n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })
const fmt2 = n => '₹' + (n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const pnlCls = n => n >= 0 ? 'pos' : 'neg'
const sign = n => (n >= 0 ? '+' : '') + fmt2(n)

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
    const iv = setInterval(load, 10000)   // live dashboard — keep it fresh
    return () => clearInterval(iv)
  }, [])

  if (!pf) return <div className="panel-body"><div className="empty">Loading...</div></div>

  const t = pf.totals
  const hasStrategies = !!pf.strategies?.length
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
          <div className="k">Open / trades</div>
          <div className="v">{t.open_positions} / {t.trades}</div>
          <div className="sub">positions / fills today</div>
        </div>
      </div>
      <h3 className="sec">By strategy — click to drill in</h3>
      {hasStrategies ? (
        <div className="table-scroll"><table>
          <thead>
            <tr>
              <th>Strategy</th>
              <th>State</th>
              <th>Capital</th>
              <th>Day P&L</th>
              <th>Day ROI</th>
              <th>Open</th>
              <th>Trades</th>
            </tr>
          </thead>
          <tbody>
            {pf.strategies.map(s => (
              <tr key={s.id} style={{ cursor: 'pointer' }} onClick={() => onStrategyClick(s.id)}>
                <td style={{ fontFamily: 'var(--body)', fontWeight: '600' }}>{s.name}</td>
                <td style={{ textAlign: 'left' }}>
                  <span className={`badge ${s.state}`}>{s.state.replace('DEPLOYED_', '')}</span>
                </td>
                <td>{fmt(s.allocated_capital)}</td>
                <td className={pnlCls(s.day_pnl)}>{sign(s.day_pnl)}</td>
                <td className={pnlCls(s.day_roi_pct)}>{s.day_roi_pct}%</td>
                <td>{s.open_positions}</td>
                <td>{s.trades_today}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      ) : (
        <div className="empty">
          Nothing deployed yet — deploy a strategy for paper trading and today's
          P&amp;L will fill in here.
        </div>
      )}
    </div>
  )
}
