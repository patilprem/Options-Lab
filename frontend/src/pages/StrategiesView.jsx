import { useEffect, useMemo, useState } from 'react'

const fmt = n => '₹' + (n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })
const fmt2 = n => '₹' + (n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const pnlCls = n => (n >= 0 ? 'pos' : 'neg')
const sign = n => (n >= 0 ? '+' : '') + fmt2(n)

const STATES = ['ALL', 'RUNNING', 'DEPLOYED_PAUSED', 'VALIDATED', 'STOPPED', 'DRAFT']
const input = {
  background: 'var(--bg)', border: '1px solid var(--line)', color: 'var(--ink)',
  borderRadius: 8, padding: '7px 10px', fontFamily: 'var(--mono)', fontSize: 13,
}

export default function StrategiesView({ strategies, onSelect, onNew }) {
  const [q, setQ] = useState('')
  const [state, setState] = useState('ALL')
  const [live, setLive] = useState({})   // id -> {day_pnl, trades_today, open_positions}

  useEffect(() => {
    const load = async () => {
      try {
        const pf = await fetch('/portfolio/today').then(r => r.json())
        const m = {}
        ;(pf.strategies || []).forEach(s => { m[s.id] = s })
        setLive(m)
      } catch { /* dashboard P&L is a bonus; the list still works */ }
    }
    load()
    const iv = setInterval(load, 10000)
    return () => clearInterval(iv)
  }, [])

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return strategies.filter(s => {
      if (state !== 'ALL' && s.state !== state) return false
      if (!needle) return true
      return (s.name || '').toLowerCase().includes(needle) ||
             (s.meta?.underlying || '').toLowerCase().includes(needle)
    })
  }, [strategies, q, state])

  const counts = useMemo(() => {
    const c = { total: strategies.length, running: 0, deployed: 0 }
    strategies.forEach(s => {
      if (s.state === 'RUNNING') c.running++
      if (s.state === 'RUNNING' || s.state === 'DEPLOYED_PAUSED') c.deployed++
    })
    return c
  }, [strategies])

  return (
    <div className="panel-body">
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 14 }}>
        <input
          style={{ ...input, flex: '1 1 220px', minWidth: 180 }}
          placeholder="Search name or underlying…"
          value={q}
          onChange={e => setQ(e.target.value)}
        />
        <select style={input} value={state} onChange={e => setState(e.target.value)}>
          {STATES.map(s => <option key={s} value={s}>{s === 'ALL' ? 'All states' : s.replace('DEPLOYED_', '')}</option>)}
        </select>
        <button className="btn btn-primary" onClick={onNew}>+ New strategy</button>
      </div>

      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 10 }}>
        {rows.length} of {counts.total} shown · {counts.running} running · {counts.deployed} deployed
      </div>

      {!strategies.length ? (
        <div className="empty">No strategies yet. Paste your first one to get started.</div>
      ) : !rows.length ? (
        <div className="empty">No strategies match “{q}”{state !== 'ALL' ? ` in ${state}` : ''}.</div>
      ) : (
        <div className="table-scroll"><table>
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Underlying</th>
              <th>State</th>
              <th>Capital</th>
              <th>Day P&L</th>
              <th>Open</th>
              <th>Trades</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(s => {
              const l = live[s.id]
              return (
                <tr key={s.id} style={{ cursor: 'pointer' }} onClick={() => onSelect(s.id)}>
                  <td style={{ fontFamily: 'var(--body)', fontWeight: 600 }}>{s.name}</td>
                  <td>{s.meta?.underlying || '—'}</td>
                  <td style={{ textAlign: 'left' }}>
                    <span className={`badge ${s.state}`}>{s.state.replace('DEPLOYED_', '')}</span>
                  </td>
                  <td>{fmt(s.allocated_capital)}</td>
                  <td className={l ? pnlCls(l.day_pnl) : ''}>{l ? sign(l.day_pnl) : '—'}</td>
                  <td>{l ? l.open_positions : '—'}</td>
                  <td>{l ? l.trades_today : '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table></div>
      )}
    </div>
  )
}
