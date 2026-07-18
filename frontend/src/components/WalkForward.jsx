import { useState } from 'react'
import { Line } from 'react-chartjs-2'
import {
  Chart, LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Filler,
} from 'chart.js'

Chart.register(LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Filler)

const css = getComputedStyle(document.documentElement)
const col = (name, fallback) => (css.getPropertyValue(name).trim() || fallback)

// default 3-month window ending today
const iso = d => d.toISOString().slice(0, 10)
const today = new Date()
const threeMonthsAgo = new Date(today.getTime() - 90 * 864e5)

const field = { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }
const input = {
  background: 'var(--bg)', border: '1px solid var(--line)', color: 'var(--ink)',
  borderRadius: 6, padding: '6px 8px', fontFamily: 'var(--mono)', fontSize: 13,
}
const cell = { padding: '6px 10px', borderBottom: '1px solid var(--line)', fontFamily: 'var(--mono)' }

export default function WalkForward({ id, params = {} }) {
  const [form, setForm] = useState({
    from_date: iso(threeMonthsAgo), to_date: iso(today),
    capital: 600000, folds: 3, is_frac: 0.7, metric: 'return_pct',
  })
  const [grid, setGrid] = useState({})   // param name -> "0.2, 0.3, 0.5"
  const [result, setResult] = useState(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState(null)

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const run = async () => {
    const param_grid = {}
    const badTokens = []
    for (const [k, v] of Object.entries(grid)) {
      if (!String(v).trim()) continue
      // tolerate a typed list literal like "[20, 22, 25]" — strip the
      // brackets rather than let them corrupt the split (a bare "[20" and
      // "25]" both fail Number() and get silently dropped, which once
      // collapsed a 3-value sweep down to a single value with NO error
      // shown — a walk-forward result that looked legitimate but had
      // silently swept just the strategy's own hardcoded default).
      const tokens = String(v).replace(/[[\]()]/g, '')
        .split(',').map(s => s.trim()).filter(Boolean)
      const nums = tokens.map(Number)
      const bad = tokens.filter((t, i) => Number.isNaN(nums[i]))
      if (bad.length) badTokens.push(`${k}: "${bad.join('", "')}"`)
      else if (nums.length) param_grid[k] = nums
    }
    if (badTokens.length) {
      setError(`Can't parse as numbers — ${badTokens.join('; ')}. Use comma-separated values, e.g. 20, 22, 25.`)
      return
    }
    setRunning(true); setError(null); setResult(null)
    try {
      const res = await fetch(`/strategies/${id}/walkforward`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...form, capital: Number(form.capital), folds: Number(form.folds),
          is_frac: Number(form.is_frac), param_grid,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Request failed')
      if (data.status !== 'ok') throw new Error(data.message || 'No result')
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setRunning(false)
    }
  }

  const curve = result?.aggregate_oos?.equity_curve || []
  const chartData = {
    labels: curve.map(p => p.date),
    datasets: [{
      data: curve.map(p => p.equity),
      borderColor: col('--lime', '#B8F04A'), borderWidth: 2,
      backgroundColor: 'rgba(184,240,74,0.08)', fill: true,
      pointRadius: 0, tension: 0.15,
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

  const agg = result?.aggregate_oos
  const pos = v => (v >= 0 ? 'var(--green)' : 'var(--red)')

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <p style={{ color: 'var(--muted)', fontSize: 13, margin: 0 }}>
        Optimize params in-sample per fold, then measure them out-of-sample. The
        aggregate OOS curve is the un-fitted estimate of live performance.
      </p>

      {/* controls */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(120px,1fr))', gap: 10 }}>
        <label style={field}>From<input style={input} type="date" value={form.from_date} onChange={e => set('from_date', e.target.value)} /></label>
        <label style={field}>To<input style={input} type="date" value={form.to_date} onChange={e => set('to_date', e.target.value)} /></label>
        <label style={field}>Capital ₹<input style={input} type="number" value={form.capital} onChange={e => set('capital', e.target.value)} /></label>
        <label style={field}>Folds<input style={input} type="number" min="2" max="12" value={form.folds} onChange={e => set('folds', e.target.value)} /></label>
        <label style={field}>In-sample frac<input style={input} type="number" step="0.05" min="0.3" max="0.9" value={form.is_frac} onChange={e => set('is_frac', e.target.value)} /></label>
        <label style={field}>Optimize
          <select style={input} value={form.metric} onChange={e => set('metric', e.target.value)}>
            <option value="return_pct">Return %</option>
            <option value="sharpe">Sharpe</option>
            <option value="total_pnl">Total P&amp;L</option>
            <option value="win_rate_pct">Win rate</option>
          </select>
        </label>
      </div>

      {/* param grid sweep */}
      {Object.keys(params).length > 0 && (
        <div>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 6 }}>
            Param sweep (comma-separated values to grid-search; blank = keep default)
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(160px,1fr))', gap: 10 }}>
            {Object.entries(params).map(([k, v]) => (
              <label key={k} style={field}>
                {k} <span style={{ color: 'var(--faint)' }}>(def {String(v)})</span>
                <input style={input} placeholder={`e.g. ${v}, ...`} value={grid[k] || ''}
                  onChange={e => setGrid(g => ({ ...g, [k]: e.target.value }))} />
              </label>
            ))}
          </div>
        </div>
      )}

      <div>
        <button className="btn btn-primary" onClick={run} disabled={running}>
          {running ? 'Running…' : 'Run walk-forward'}
        </button>
      </div>

      {error && <div style={{ color: 'var(--red)', fontFamily: 'var(--mono)', fontSize: 13 }}>{error}</div>}

      {agg && (
        <>
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
            <Stat label="OOS return" value={`${agg.return_pct}%`} color={pos(agg.return_pct)} />
            <Stat label="Max drawdown" value={`${agg.max_drawdown_pct}%`} color="var(--amber)" />
            <Stat label="OOS days" value={agg.days} />
            <Stat label="Backtests run" value={result.runs} />
          </div>

          <div style={{ height: 240 }}>
            <Line data={chartData} options={chartOpts} />
          </div>

          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
                  <th style={cell}>Fold</th><th style={cell}>In-sample</th>
                  <th style={cell}>Out-of-sample</th><th style={cell}>Best params</th>
                  <th style={cell}>OOS return</th><th style={cell}>Trades</th>
                </tr>
              </thead>
              <tbody>
                {result.folds.map(f => (
                  <tr key={f.fold}>
                    <td style={cell}>{f.fold}</td>
                    <td style={cell}>{f.is[0]} → {f.is[1]}</td>
                    <td style={cell}>{f.oos[0]} → {f.oos[1]}</td>
                    <td style={cell}>{Object.entries(f.best_params).map(([k, v]) => `${k}=${v}`).join(' ') || '—'}</td>
                    <td style={{ ...cell, color: pos(f.oos_summary?.return_pct || 0) }}>{f.oos_summary?.return_pct ?? 0}%</td>
                    <td style={cell}>{f.oos_summary?.n_trades ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
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
