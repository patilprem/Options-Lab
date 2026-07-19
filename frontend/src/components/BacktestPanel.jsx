import { useState, useEffect } from 'react'
import { Line } from 'react-chartjs-2'
import InsightsPanel from './InsightsPanel'
import {
  Chart, LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Filler,
} from 'chart.js'

Chart.register(LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Filler)

const css = getComputedStyle(document.documentElement)
const col = (name, fallback) => (css.getPropertyValue(name).trim() || fallback)
const inr = n => '₹' + Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })

const field = { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }
const input = {
  background: 'var(--bg)', border: '1px solid var(--line)', color: 'var(--ink)',
  borderRadius: 6, padding: '6px 8px', fontFamily: 'var(--mono)', fontSize: 13,
}
const cell = { padding: '6px 10px', borderBottom: '1px solid var(--line)', fontFamily: 'var(--mono)', textAlign: 'left' }

export default function BacktestPanel({ id, underlying }) {
  const [form, setForm] = useState({ from_date: '', to_date: '', capital: 1000000 })
  const [coverage, setCoverage] = useState([])
  const [result, setResult] = useState(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch('/data/coverage').then(r => r.json()).then(d => {
      const cov = d.coverage || []
      setCoverage(cov)
      const mine = cov.find(c => c.underlying === underlying) || cov[0]
      if (mine) setForm(f => ({ ...f, from_date: mine.from, to_date: mine.to }))
    }).catch(() => {})
  }, [underlying])

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const run = async () => {
    setRunning(true); setError(null); setResult(null)
    try {
      const res = await fetch(`/strategies/${id}/backtest`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...form, capital: Number(form.capital) }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Request failed')
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setRunning(false)
    }
  }

  const daily = result?.status === 'ok' ? (result.daily || []) : []
  const chartData = {
    labels: daily.map(d => d.date),
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

  const s = result?.summary
  const pos = v => (v >= 0 ? 'var(--green)' : 'var(--red)')
  const covText = coverage.length
    ? coverage.map(c => `${c.underlying} ${c.from} → ${c.to}`).join(', ')
    : 'none — backfill data first (Data tab)'

  const trades = result?.trades || []
  const largest = [...trades].sort((a, b) => Math.abs(b.pnl) - Math.abs(a.pnl)).slice(0, 6)

  const attribution = result?.attribution || {}
  const ATTR_LABELS = { iv_rank: 'IV rank', index_bias: 'Index bias', pcr_oi: 'PCR (OI)' }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ fontSize: 12, color: 'var(--muted)' }}>Available data: {covText}</div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(120px,1fr))', gap: 10 }}>
        <label style={field}>From<input style={input} type="date" value={form.from_date} onChange={e => set('from_date', e.target.value)} /></label>
        <label style={field}>To<input style={input} type="date" value={form.to_date} onChange={e => set('to_date', e.target.value)} /></label>
        <label style={field}>Capital ₹<input style={input} type="number" value={form.capital} onChange={e => set('capital', e.target.value)} /></label>
      </div>

      <div>
        <button className="btn btn-primary" onClick={run} disabled={running || !form.from_date}>
          {running ? 'Running…' : 'Run backtest'}
        </button>
      </div>

      {error && <div style={{ color: 'var(--red)', fontFamily: 'var(--mono)', fontSize: 13 }}>{error}</div>}

      {result?.status === 'data_unavailable' && (
        <div className="banner warn" style={{ padding: '10px 12px', borderRadius: 8 }}>
          {result.message}
        </div>
      )}

      {s && (
        <>
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
            <Stat label="Total P&L" value={inr(s.total_pnl)} color={pos(s.total_pnl)} />
            <Stat label="Return" value={`${s.return_pct}%`} color={pos(s.return_pct)} />
            <Stat label="Max drawdown" value={`${s.max_drawdown_pct}%`} color="var(--amber)" />
            <Stat label="Sharpe" value={s.sharpe} />
            <Stat label="Trades" value={s.n_trades} />
            <Stat label="Win rate" value={`${s.win_rate_pct}%`} />
            <Stat label="Fees" value={inr(s.total_fees)} />
          </div>

          <div style={{ height: 240 }}>
            <Line data={chartData} options={chartOpts} />
          </div>

          <div className="table-scroll">
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
                  <th style={cell}>Date</th><th style={cell}>Realized</th>
                  <th style={cell}>Fees</th><th style={cell}>Equity EOD</th><th style={cell}>Trades</th>
                </tr>
              </thead>
              <tbody>
                {daily.map((d, i) => (
                  <tr key={i}>
                    <td style={cell}>{d.date}</td>
                    <td style={{ ...cell, color: pos(d.realized) }}>{inr(d.realized)}</td>
                    <td style={cell}>{inr(d.fees)}</td>
                    <td style={cell}>{inr(d.equity_eod)}</td>
                    <td style={cell}>{d.trades}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {result?.insights && <InsightsPanel insights={result.insights} />}

          {Object.keys(attribution).length > 0 && (
            <Section title="Signal attribution — which data state at entry paid?">
              {Object.entries(attribution).map(([key, buckets]) => (
                <div key={key} style={{ marginBottom: 14 }}>
                  <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 6 }}>
                    {ATTR_LABELS[key] || key}
                  </div>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                    <thead>
                      <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
                        <th style={cell}>Bucket</th><th style={cell}>n</th>
                        <th style={cell}>Win rate</th><th style={cell}>Avg P&L</th>
                        <th style={cell}>Total P&L</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(buckets)
                        .filter(([b]) => b !== 'overall')
                        .sort((a, b) => b[1].n - a[1].n)
                        .map(([bucket, v]) => (
                          <tr key={bucket}>
                            <td style={cell}>{bucket}</td>
                            <td style={cell}>{v.n}</td>
                            <td style={cell}>{v.win_rate}%</td>
                            <td style={{ ...cell, color: pos(v.avg_pnl) }}>{inr(v.avg_pnl)}</td>
                            <td style={{ ...cell, color: pos(v.total_pnl) }}>{inr(v.total_pnl)}</td>
                          </tr>
                        ))}
                      {buckets.overall && (
                        <tr style={{ color: 'var(--muted)' }}>
                          <td style={cell}>overall</td>
                          <td style={cell}>{buckets.overall.n}</td>
                          <td style={cell}>{buckets.overall.win_rate}%</td>
                          <td style={cell}>{inr(buckets.overall.avg_pnl)}</td>
                          <td style={cell}>{inr(buckets.overall.total_pnl)}</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              ))}
            </Section>
          )}

          {largest.length > 0 && (
            <Section title="Largest trades — is P&L concentrated in a few?">
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead>
                  <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
                    <th style={cell}>Entry</th><th style={cell}>Exit</th>
                    <th style={cell}>Tag</th><th style={cell}>Exit reason</th>
                    <th style={cell}>P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {largest.map((t, i) => (
                    <tr key={i}>
                      <td style={cell}>{t.entry_ts}</td>
                      <td style={cell}>{t.exit_ts || '—'}</td>
                      <td style={cell}>{t.tag}</td>
                      <td style={cell}>{t.exit_reason}</td>
                      <td style={{ ...cell, color: pos(t.pnl) }}>{inr(t.pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Section>
          )}
        </>
      )}
    </div>
  )
}

function Section({ title, children }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ fontSize: 13, color: 'var(--ink)', fontWeight: 600 }}>{title}</div>
      <div className="table-scroll">{children}</div>
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
