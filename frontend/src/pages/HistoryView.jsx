import { Fragment, useState, useEffect } from 'react'

const SCANNER_ID = 'SCANNER'
const fmt2 = n => '₹' + (n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const sign = n => (n >= 0 ? '+' : '') + fmt2(n)
const pnlCls = n => n >= 0 ? 'pos' : 'neg'
const timeOf = ts => (ts || '').slice(11, 16) || ts

export default function HistoryView({ strategies }) {
  const today = new Date().toISOString().slice(0, 10)
  const past = new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10)
  const [from, setFrom] = useState(past)
  const [to, setTo] = useState(today)
  const [sid, setSid] = useState('')
  const [mode, setMode] = useState('')
  const [days, setDays] = useState(null)
  // per-day drill-down: date -> {loading, trades} once the user taps "Show trades"
  const [open, setOpen] = useState({})

  const load = async () => {
    try {
      const q = new URLSearchParams({ from_date: from, to_date: to, strategy_id: sid, mode })
      const data = await fetch(`/trades/daily?${q}`).then(r => r.json())
      setDays(data.days || [])
      setOpen({})   // filters changed — collapse anything previously expanded
    } catch (e) {
      console.error(e)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const toggleDay = async (date) => {
    const wasOpen = !!open[date]
    if (wasOpen) {
      setOpen(o => { const { [date]: _drop, ...rest } = o; return rest })
      return
    }
    setOpen(o => ({ ...o, [date]: { loading: true, trades: [] } }))
    try {
      const q = new URLSearchParams({ from_date: date, to_date: date, strategy_id: sid, mode })
      const data = await fetch(`/trades?${q}`).then(r => r.json())
      setOpen(o => ({ ...o, [date]: { loading: false, trades: data.trades || [] } }))
    } catch (e) {
      console.error(e)
      setOpen(o => ({ ...o, [date]: { loading: false, trades: [] } }))
    }
  }

  return (
    <div className="panel-body">
      <div className="form-grid">
        <div>
          <label className="f">From</label>
          <input type="date" value={from} onChange={e => setFrom(e.target.value)} />
        </div>
        <div>
          <label className="f">To</label>
          <input type="date" value={to} onChange={e => setTo(e.target.value)} />
        </div>
        <div>
          <label className="f">Source</label>
          <select value={sid} onChange={e => setSid(e.target.value)}>
            <option value="">All</option>
            <option value={SCANNER_ID}>Scanner Auto-Trader</option>
            {strategies.map(s => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="f">Mode</label>
          <select value={mode} onChange={e => setMode(e.target.value)}>
            <option value="">All</option>
            <option>PAPER</option>
            <option>LIVE</option>
          </select>
        </div>
        <button className="btn btn-primary" onClick={load}>Filter</button>
      </div>

      {days === null ? (
        <div className="empty">Loading...</div>
      ) : days.length ? (
        <div className="table-scroll"><table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Trades</th>
              <th>Net P&amp;L</th>
              <th>Gross P&amp;L</th>
              <th>Charges</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {days.map(d => {
              const day = open[d.date]
              return (
                <Fragment key={d.date}>
                  <tr>
                    <td style={{ fontFamily: 'var(--body)', fontWeight: '600' }}>{d.date}</td>
                    <td>{d.trades}</td>
                    <td className={pnlCls(d.net_pnl)}>{sign(d.net_pnl)}</td>
                    <td className={pnlCls(d.gross_pnl)}>{sign(d.gross_pnl)}</td>
                    <td>{fmt2(d.fees)}</td>
                    <td>
                      <button className="btn" onClick={() => toggleDay(d.date)}>
                        {day ? 'Hide' : 'Show trades'}
                      </button>
                    </td>
                  </tr>
                  {day && (
                    <tr>
                      <td colSpan={6} style={{ padding: 0 }}>
                        {day.loading ? (
                          <div className="empty">Loading trades...</div>
                        ) : day.trades.length ? (
                          <div className="table-scroll"><table>
                            <thead>
                              <tr>
                                <th>Time</th>
                                <th>Source</th>
                                <th>Mode</th>
                                <th>Contract</th>
                                <th>Side</th>
                                <th>Qty</th>
                                <th>Price</th>
                                <th>Fees</th>
                                <th>Reason</th>
                              </tr>
                            </thead>
                            <tbody>
                              {day.trades.map((t, i) => (
                                <tr key={i}>
                                  <td>{timeOf(t.ts)}</td>
                                  <td>{t.strategy || ''}</td>
                                  <td style={{ textAlign: 'left' }}>
                                    <span className={`badge ${t.mode === 'LIVE' ? 'LIVE' : 'VALIDATED'}`}>{t.mode}</span>
                                  </td>
                                  <td style={{ textAlign: 'left' }}>{t.contract}</td>
                                  <td className={t.side === 'BUY' ? 'pos' : 'neg'}>{t.side}</td>
                                  <td>{t.qty}</td>
                                  <td>{fmt2(t.price)}</td>
                                  <td>{fmt2(t.fees)}</td>
                                  <td style={{ textAlign: 'left' }}>{t.reason}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table></div>
                        ) : (
                          <div className="empty">No trades found for this day.</div>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table></div>
      ) : (
        <div className="empty">No trades in this range.</div>
      )}
    </div>
  )
}
