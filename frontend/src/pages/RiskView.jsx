import { useState, useEffect, useCallback } from 'react'

const inr = n => '₹' + Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })
const input = {
  background: 'var(--bg)', border: '1px solid var(--line)', color: 'var(--ink)',
  borderRadius: 6, padding: '6px 8px', fontFamily: 'var(--mono)', fontSize: 13, width: 120,
}
const cell = { padding: '6px 10px', borderBottom: '1px solid var(--line)', fontFamily: 'var(--mono)', textAlign: 'left' }
const th = { ...cell, color: 'var(--muted)', fontWeight: 500 }

export default function RiskView({ showToast }) {
  const [snap, setSnap] = useState(null)
  const [maxLoss, setMaxLoss] = useState('')
  const [defCap, setDefCap] = useState('')

  const load = useCallback(async () => {
    try {
      const d = await fetch('/risk').then(r => r.json())
      setSnap(d)
      setMaxLoss(String(d.settings.max_daily_loss || ''))
      setDefCap(String(d.settings.default_loss_cap || ''))
    } catch { showToast && showToast('Failed to load risk') }
  }, [showToast])

  useEffect(() => {
    load()
    const iv = setInterval(load, 10000)
    return () => clearInterval(iv)
  }, [load])

  const saveLimits = async () => {
    await fetch('/risk/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ max_daily_loss: Number(maxLoss) || 0, default_loss_cap: Number(defCap) || 0 }),
    })
    showToast && showToast('Risk limits saved')
    load()
  }

  if (!snap || !snap.portfolio) return <div className="empty">Loading risk…</div>
  const p = snap.portfolio
  const barColor = p.breached ? 'var(--red)' : p.loss_used_pct > 70 ? 'var(--amber)' : 'var(--green)'

  return (
    <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {p.breached && (
        <div style={{ background: 'var(--red-tint)', border: '1px solid var(--red)', color: 'var(--red)', padding: '10px 12px', borderRadius: 8, fontWeight: 600 }}>
          ⚠ Portfolio max daily loss breached — all strategies auto-paused.
        </div>
      )}

      {/* portfolio tiles */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 14 }}>
        <Tile label="Allocated" value={inr(p.allocated)} />
        <Tile label="Margin used" value={inr(p.margin_used)} sub={`${p.margin_util_pct}% utilized`} />
        <Tile label="Day P&L" value={inr(p.day_pnl)} color={p.day_pnl >= 0 ? 'var(--green)' : 'var(--red)'} />
        <Tile label="Max daily loss" value={p.max_daily_loss ? inr(p.max_daily_loss) : 'off'} />
      </div>

      {/* utilization + loss-budget bars */}
      <Bar label="Margin utilization" pct={p.margin_util_pct} color={p.margin_util_pct > 85 ? 'var(--red)' : 'var(--lime)'} />
      {p.max_daily_loss > 0 && (
        <Bar label={`Daily loss budget used (${p.loss_used_pct}%)`} pct={p.loss_used_pct} color={barColor} />
      )}

      {/* limits editor */}
      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
          Portfolio max daily loss ₹
          <input style={input} type="number" value={maxLoss} onChange={e => setMaxLoss(e.target.value)} placeholder="0 = off" />
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
          Default per-strategy cap ₹
          <input style={input} type="number" value={defCap} onChange={e => setDefCap(e.target.value)} placeholder="0 = off" />
        </label>
        <button className="btn btn-primary" onClick={saveLimits}>Save limits</button>
      </div>

      {/* per-strategy */}
      <Section title="Strategies">
        <Table head={['Strategy', 'State', 'Day P&L', 'Margin', 'Loss cap', '']}
          rows={snap.strategies.map(s => [
            s.name, s.state.replace('DEPLOYED_', ''),
            <span style={{ color: s.day_pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>{inr(s.day_pnl)}</span>,
            inr(s.margin_used), s.loss_cap ? inr(s.loss_cap) : '—',
            s.cap_breached ? <span style={{ color: 'var(--red)' }}>breached</span> : '',
          ])} empty="No strategies deployed." />
      </Section>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(280px,1fr))', gap: 20 }}>
        <Section title="Exposure by underlying">
          <Table head={['Underlying', 'Positions', 'Net qty', 'Premium']}
            rows={snap.exposure_by_underlying.map(r => [r.underlying, r.positions, r.net_qty, inr(r.premium)])}
            empty="No open positions." />
        </Section>
        <Section title="Exposure by expiry">
          <Table head={['Expiry', 'Positions', 'Net qty', 'Premium']}
            rows={snap.exposure_by_expiry.map(r => [r.expiry, r.positions, r.net_qty, inr(r.premium)])}
            empty="No open positions." />
        </Section>
      </div>
    </div>
  )
}

function Tile({ label, value, sub, color }) {
  return (
    <div style={{ background: 'var(--glass)', border: '1px solid var(--line)', borderRadius: 10, padding: 14 }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: 0.5 }}>{label}</div>
      <div style={{ fontSize: 22, fontFamily: 'var(--disp)', color: color || 'var(--ink)' }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--faint)' }}>{sub}</div>}
    </div>
  )
}

function Bar({ label, pct, color }) {
  return (
    <div>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>{label}</div>
      <div style={{ height: 8, background: 'var(--bg)', borderRadius: 4, overflow: 'hidden', border: '1px solid var(--line)' }}>
        <div style={{ width: `${Math.min(100, Math.max(0, pct))}%`, height: '100%', background: color }} />
      </div>
    </div>
  )
}

function Section({ title, children }) {
  return (
    <div>
      <div style={{ fontSize: 12, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 8 }}>{title}</div>
      {children}
    </div>
  )
}

function Table({ head, rows, empty }) {
  if (!rows.length) return <div className="empty" style={{ padding: 12 }}>{empty}</div>
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead><tr>{head.map((h, i) => <th key={i} style={th}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j} style={cell}>{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  )
}
