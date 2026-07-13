import { useState, useEffect, useCallback } from 'react'
import { Line } from 'react-chartjs-2'
import {
  Chart, LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Filler,
} from 'chart.js'

Chart.register(LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Filler)

const css = getComputedStyle(document.documentElement)
const col = (name, fallback) => (css.getPropertyValue(name).trim() || fallback)
const inr = n => '₹' + Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })
const cell = { padding: '6px 10px', borderBottom: '1px solid var(--line)', fontFamily: 'var(--mono)', textAlign: 'left' }
const th = { ...cell, color: 'var(--muted)', fontWeight: 500 }

export default function PaperPanel({ id }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/strategies/${id}/performance`)
      const d = await res.json()
      if (!res.ok) throw new Error(d.detail || 'failed to load')
      setData(d)
    } catch (e) {
      setError(e.message)
    }
  }, [id])

  useEffect(() => {
    load()
    const iv = setInterval(load, 10000)   // live while paper trading runs
    return () => clearInterval(iv)
  }, [load])

  if (error) return <div style={{ color: 'var(--red)', fontFamily: 'var(--mono)', fontSize: 13 }}>{error}</div>
  if (!data) return <div className="empty">Loading paper performance…</div>

  const daily = data.daily || []
  const deployed = data.day_pnl !== null && data.day_pnl !== undefined
  const pos = v => (v >= 0 ? 'var(--green)' : 'var(--red)')

  const chartData = {
    labels: daily.map(d => d.trade_date),
    datasets: [{
      data: daily.map(d => d.equity_eod),
      borderColor: col('--lime', '#B8F04A'), borderWidth: 2,
      backgroundColor: 'rgba(184,240,74,0.08)', fill: true, pointRadius: 0, tension: 0.15,
    }],
  }
  const chartOpts = {
    responsive: true, maintainAspectRatio: false,
    plugins: { tooltip: { intersect: false, mode: 'index' } },
    scales: {
      x: { ticks: { color: col('--muted', '#889'), maxTicksLimit: 8 }, grid: { color: col('--line', '#223') } },
      y: { ticks: { color: col('--muted', '#889') }, grid: { color: col('--line', '#223') } },
    },
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {!deployed && (
        <div className="banner info" style={{ padding: '10px 12px', borderRadius: 8 }}>
          Not deployed for paper trading. Click <strong>Paper trade</strong> above to deploy —
          live performance then builds up day by day.
        </div>
      )}

      <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
        <Stat label="Day P&L" value={deployed ? inr(data.day_pnl) : '—'} color={deployed ? pos(data.day_pnl) : undefined} />
        <Stat label="Day ROI" value={data.day_roi_pct != null ? `${data.day_roi_pct}%` : '—'} color={data.day_roi_pct != null ? pos(data.day_roi_pct) : undefined} />
        <Stat label="ROI on margin" value={data.day_roi_on_margin_pct != null ? `${data.day_roi_on_margin_pct}%` : '—'} />
        <Stat label="Margin used" value={inr(data.margin_used)} />
        <Stat label="Allocated" value={inr(data.allocated_capital)} />
      </div>

      {daily.length > 0 ? (
        <>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>Paper equity ({daily.length} days)</div>
          <div style={{ height: 220 }}><Line data={chartData} options={chartOpts} /></div>
        </>
      ) : (
        <div className="empty" style={{ padding: 12 }}>No paper history yet — it accrues one row per trading day.</div>
      )}

      <Section title={`Open positions (${data.open_positions.length})`}>
        {data.open_positions.length ? (
          <Table head={['Tag', 'Type', 'Strike', 'Expiry', 'Qty', 'Entry', 'MTM', 'Unrealized']}
            rows={data.open_positions.map(p => [
              p.tag || '—', p.type, p.strike, p.expiry, p.qty, p.entry, p.mtm,
              <span style={{ color: pos(p.unrealized) }}>{inr(p.unrealized)}</span>,
            ])} />
        ) : <div className="empty" style={{ padding: 12 }}>No open positions.</div>}
      </Section>

      <Section title={`Trades today (${data.trades_today.length})`}>
        {data.trades_today.length ? (
          <Table head={['Time', 'Contract', 'Side', 'Qty', 'Price', 'Fees', 'Reason']}
            rows={data.trades_today.map(t => [
              String(t.ts || '').slice(11, 19), t.contract, t.side, t.qty, t.price, t.fees, t.reason,
            ])} />
        ) : <div className="empty" style={{ padding: 12 }}>No trades today.</div>}
      </Section>

      {daily.length > 0 && (
        <Section title="Daily P&L">
          <Table head={['Date', 'Realized', 'Unrealized', 'Fees', 'Equity EOD']}
            rows={daily.slice().reverse().map(d => [
              d.trade_date,
              <span style={{ color: pos(d.realized) }}>{inr(d.realized)}</span>,
              inr(d.unrealized), inr(d.fees), inr(d.equity_eod),
            ])} />
        </Section>
      )}
    </div>
  )
}

function Stat({ label, value, color }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: 0.5 }}>{label}</div>
      <div style={{ fontSize: 22, fontFamily: 'var(--disp)', color: color || 'var(--ink)' }}>{value}</div>
    </div>
  )
}

function Section({ title, children }) {
  return (
    <div>
      <div style={{ fontSize: 12, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>{title}</div>
      {children}
    </div>
  )
}

function Table({ head, rows }) {
  return (
    <div style={{ overflowX: 'auto', maxHeight: 260 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead><tr>{head.map((h, i) => <th key={i} style={th}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j} style={cell}>{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  )
}
