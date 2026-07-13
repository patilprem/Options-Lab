import { useState, useEffect } from 'react'

export default function HistoryView({ strategies }) {
  const [trades, setTrades] = useState([])
  const today = new Date().toISOString().slice(0, 10)
  const past = new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10)
  const [from, setFrom] = useState(past)
  const [to, setTo] = useState(today)
  const [sid, setSid] = useState('')
  const [mode, setMode] = useState('')

  const load = async () => {
    try {
      const q = new URLSearchParams({ from_date: from, to_date: to, strategy_id: sid, mode })
      const data = await fetch(`/trades?${q}`).then(r => r.json())
      setTrades(data.trades || [])
    } catch (e) {
      console.error(e)
    }
  }

  useEffect(() => {
    load()
  }, [])

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
          <label className="f">Strategy</label>
          <select value={sid} onChange={e => setSid(e.target.value)}>
            <option value="">All</option>
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
      {trades.length ? (
        <div className="table-scroll"><table>
          <thead>
            <tr>
              <th>Date &amp; time</th>
              <th>Strategy</th>
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
            {trades.map((t, i) => (
              <tr key={i}>
                <td>{(t.ts || '').slice(0, 16)}</td>
                <td>{t.strategy || ''}</td>
                <td style={{ textAlign: 'left' }}>
                  <span className={`badge ${t.mode === 'LIVE' ? 'LIVE' : 'VALIDATED'}`}>{t.mode}</span>
                </td>
                <td style={{ textAlign: 'left' }}>{t.contract}</td>
                <td className={t.side === 'BUY' ? 'pos' : 'neg'}>{t.side}</td>
                <td>{t.qty}</td>
                <td>₹{t.price}</td>
                <td>₹{t.fees}</td>
                <td style={{ textAlign: 'left' }}>{t.reason}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      ) : (
        <div className="empty">No trades in this range.</div>
      )}
    </div>
  )
}
