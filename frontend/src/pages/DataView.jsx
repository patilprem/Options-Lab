import { useEffect, useState, useRef } from 'react'

const UNDERLYINGS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'SENSEX']
const PERIODS = [
  ['2y', '2 years (default)'], ['1y', '1 year'], ['6m', '6 months'],
  ['3m', '3 months'], ['5y', 'Max (~5 years)'],
]
const input = {
  background: 'var(--bg)', border: '1px solid var(--line)', color: 'var(--ink)',
  borderRadius: 6, padding: '6px 8px', fontFamily: 'var(--mono)', fontSize: 13,
}

export default function DataView({ showToast }) {
  const [data, setData] = useState(null)
  const [maturity, setMaturity] = useState(null)
  const [underlying, setUnderlying] = useState('NIFTY')
  const [period, setPeriod] = useState('2y')
  const [strikes, setStrikes] = useState(5)   // ATM±N option strikes to pull
  const [status, setStatus] = useState(null)
  const poll = useRef(null)

  const wasRunning = useRef(false)
  const loadCoverage = async () => {
    try { setData(await fetch('/data/coverage').then(r => r.json())) } catch { /* */ }
    try { setMaturity(await fetch('/data/maturity').then(r => r.json())) } catch { /* */ }
  }
  const loadStatus = async () => {
    try {
      const s = await fetch('/data/backfill/status').then(r => r.json())
      setStatus(s)
      if (wasRunning.current && !s.running) {       // just finished -> refresh coverage
        loadCoverage()
        showToast && showToast(s.error ? 'Backfill failed' : 'Backfill complete')
      }
      wasRunning.current = s.running
    } catch { /* */ }
  }

  useEffect(() => {
    loadCoverage(); loadStatus()
    poll.current = setInterval(loadStatus, 3000)     // always poll so background jobs show
    return () => { if (poll.current) clearInterval(poll.current) }
  }, [])

  const startBackfill = async () => {
    try {
      const res = await fetch('/data/backfill', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ underlying, period,
                               strike_offsets: Number(strikes) || 2, interval: 5 }),
      })
      const d = await res.json()
      if (!res.ok) throw new Error(d.detail || 'failed')
      showToast && showToast(`Backfilling ${underlying} ${d.from} → ${d.to}`)
      loadStatus()
    } catch (e) {
      showToast && showToast(e.message)
    }
  }

  const recOn = data?.recording_on ?? data?.mcx_recording
  const toggleRecording = async () => {
    await fetch(`/data/recording/${recOn ? 'off' : 'on'}`, { method: 'POST' })
    showToast(`Live recording ${recOn ? 'off' : 'on'}`)
    loadCoverage()
  }

  if (!data) return <div className="panel-body"><div className="empty">Loading...</div></div>

  const running = status?.running

  return (
    <div className="panel-body">
      {data.synthetic && (
        <div className="banner warn"><strong>Synthetic market active.</strong> {data.note}</div>
      )}

      {maturity && (
        <>
          <h3 className="sec">Analysis machinery</h3>
          <div style={{ padding: 14, borderRadius: 12, border: '1px solid var(--line)', background: 'var(--glass)', marginBottom: 20 }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
              <span style={{ fontFamily: 'var(--disp)', fontWeight: 700, fontSize: 17 }}>
                {maturity.stage}
              </span>
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>{maturity.unlocks}</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '10px 0 4px' }}>
              <div style={{ flex: 1, height: 8, background: 'var(--bg)', borderRadius: 4, overflow: 'hidden', border: '1px solid var(--line)' }}>
                <div style={{ width: `${maturity.maturity_pct}%`, height: '100%', background: 'var(--lime)', transition: 'width .3s' }} />
              </div>
              <span className="num" style={{ fontSize: 12, color: 'var(--muted)' }}>
                {maturity.learning_days}/{maturity.target_days} sessions
              </span>
            </div>
            {maturity.next_stage && (
              <div style={{ fontSize: 11.5, color: 'var(--faint)', marginBottom: 8 }}>
                next: {maturity.next_stage.name} in {maturity.next_stage.days_to_go} learning day{maturity.next_stage.days_to_go === 1 ? '' : 's'}
              </div>
            )}
            <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', fontSize: 12, marginTop: 6 }}>
              <span><span className="num" style={{ fontWeight: 600 }}>{maturity.learning_days}</span> <span style={{ color: 'var(--muted)' }}>learning days</span></span>
              <span><span className="num" style={{ fontWeight: 600 }}>{maturity.underlyings.length}</span> <span style={{ color: 'var(--muted)' }}>underlyings followed</span></span>
              <span><span className="num" style={{ fontWeight: 600 }}>{(maturity.chain_rows_total || 0).toLocaleString('en-IN')}</span> <span style={{ color: 'var(--muted)' }}>chain rows learned</span></span>
              <span><span className="num" style={{ fontWeight: 600 }}>{maturity.paper_trades_observed}</span> <span style={{ color: 'var(--muted)' }}>paper fills observed</span></span>
              <span><span className="num" style={{ fontWeight: 600 }}>{maturity.hypotheses_tracked}</span> <span style={{ color: 'var(--muted)' }}>hypotheses tracked</span></span>
            </div>
            {maturity.underlyings.length > 0 && (
              <div className="table-scroll" style={{ marginTop: 10 }}><table>
                <thead>
                  <tr><th>Underlying</th><th>Sessions</th><th>First day</th><th>Last day</th><th>Chain rows</th><th>Strikes seen</th></tr>
                </thead>
                <tbody>
                  {maturity.underlyings.map(u => (
                    <tr key={u.underlying}>
                      <td style={{ fontFamily: 'var(--body)', fontWeight: 600 }}>{u.underlying}</td>
                      <td>{u.sessions}</td>
                      <td>{u.first_day}</td><td>{u.last_day}</td>
                      <td>{(u.chain_rows || 0).toLocaleString('en-IN')}</td>
                      <td>{u.strikes_seen}</td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
          </div>
        </>
      )}

      <h3 className="sec">Backtestable ranges</h3>
      {data.coverage?.length ? (
        <div className="table-scroll"><table>
          <thead>
            <tr><th>Underlying</th><th>From</th><th>To</th><th>Underlying bars</th><th>Option bars</th></tr>
          </thead>
          <tbody>
            {data.coverage.map(c => (
              <tr key={c.underlying}>
                <td style={{ fontFamily: 'var(--body)', fontWeight: '600' }}>{c.underlying}</td>
                <td>{c.from}</td><td>{c.to}</td>
                <td>{c.underlying_bars.toLocaleString('en-IN')}</td>
                <td>{c.option_bars.toLocaleString('en-IN')}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      ) : (
        <div className="empty">No real data downloaded yet.</div>
      )}

      <div style={{ height: 20 }} />
      <h3 className="sec">Backfill history</h3>
      <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
          Underlying
          <select style={input} value={underlying} onChange={e => setUnderlying(e.target.value)} disabled={running}>
            {UNDERLYINGS.map(u => <option key={u} value={u}>{u}</option>)}
          </select>
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
          Period
          <select style={input} value={period} onChange={e => setPeriod(e.target.value)} disabled={running}>
            {PERIODS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
          Strikes ATM±
          <select style={input} value={strikes} onChange={e => setStrikes(e.target.value)} disabled={running}>
            {[2, 3, 4, 5].map(n => <option key={n} value={n}>±{n}</option>)}
          </select>
        </label>
        <button className="btn btn-primary" onClick={startBackfill} disabled={running}>
          {running ? 'Backfilling…' : 'Pull history'}
        </button>
      </div>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 6 }}>
        Pulls spot + ATM±N option candles (5-min). Wider strikes = more chunks =
        slower, but multi-leg strategies (spreads/condors with far-OTM wings) can
        only backtest strikes that exist here. Re-running is incremental: chunks
        already fetched (e.g. an earlier ±2 pull) are skipped, only new offsets
        download. The expired-options endpoint is slow (~1 min/chunk) and runs in
        the background.
      </div>

      <div style={{ marginTop: 12, padding: 12, borderRadius: 8, border: '1px solid var(--line)', background: 'var(--glass)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, marginBottom: 6 }}>
          <span style={{
            width: 8, height: 8, borderRadius: '50%',
            background: running ? 'var(--lime)' : status?.error ? 'var(--red)' : 'var(--faint)',
          }} />
          <strong style={{ fontFamily: 'var(--body)' }}>
            {running ? 'Backfill running' : status?.error ? 'Last backfill failed' : status?.done ? 'Backfill idle (last run complete)' : 'Backfill idle'}
          </strong>
        </div>
        {status && (running || status.done > 0) && (
          <>
            <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 4, fontFamily: 'var(--mono)' }}>
              {status.underlying} {status.from} → {status.to} · {status.pct}% ({status.done}/{status.total} chunks){status.message ? ` · ${status.message}` : ''}
            </div>
            <div style={{ height: 8, background: 'var(--bg)', borderRadius: 4, overflow: 'hidden', border: '1px solid var(--line)' }}>
              <div style={{ width: `${status.pct || 0}%`, height: '100%', background: status.error ? 'var(--red)' : 'var(--lime)', transition: 'width .3s' }} />
            </div>
          </>
        )}
        {!running && !status?.done && <div style={{ fontSize: 12, color: 'var(--faint)' }}>No backfill running.</div>}
        {status?.error && <div style={{ color: 'var(--red)', fontSize: 12, marginTop: 4 }}>{status.error}</div>}
      </div>

      <div style={{ height: 20 }} />
      <h3 className="sec">Live market recording</h3>
      <div style={{ padding: 14, borderRadius: 12, border: '1px solid var(--line)', background: 'var(--glass)', maxWidth: 860 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
          <span className="chip">
            <span className={`dot ${recOn ? 'ok' : 'bad'}`}></span>
            {recOn ? 'Recording' : 'Recording off'}
          </span>
          <button className="pill" onClick={toggleRecording}
                  style={recOn
                    ? { color: 'var(--red)', borderColor: 'rgba(240,113,107,0.4)' }
                    : { color: 'var(--lime)', borderColor: 'rgba(184,240,74,0.4)' }}>
            {recOn ? 'Turn off' : 'Turn on'}
          </button>
        </div>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 10, lineHeight: 1.6 }}>
          Every 5 min during market hours the full option chain of{' '}
          <span style={{ color: 'var(--ink)', fontFamily: 'var(--mono)' }}>
            {(data.recording_underlyings || []).join(', ')}
          </span>{' '}
          is snapshotted — {(data.recording_fields || []).join(' · ')} — plus live spot
          candles. This builds the OI/IV footprint dataset the historical backfill
          can't provide (and MCX history Dhan doesn't offer).
        </div>
        {data.recording?.length ? (
          <div className="table-scroll"><table>
            <thead>
              <tr><th>Underlying</th><th>Chain rows today</th><th>Strikes</th>
                  <th>Expiries</th><th>Last snapshot</th><th>Spot bars today</th></tr>
            </thead>
            <tbody>
              {data.recording.map(r => (
                <tr key={r.underlying}>
                  <td style={{ fontFamily: 'var(--body)', fontWeight: 600 }}>{r.underlying}</td>
                  <td>{(r.chain_rows_today || 0).toLocaleString('en-IN')}</td>
                  <td>{r.strikes || 0}</td>
                  <td>{r.expiries || 0}</td>
                  <td>{r.last_snapshot ? r.last_snapshot.slice(11, 16) : '—'}</td>
                  <td>{r.spot_bars_today || 0}{r.last_spot_bar ? ` (last ${r.last_spot_bar.slice(11, 16)})` : ''}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        ) : (
          <div style={{ fontSize: 12, color: 'var(--faint)', padding: '8px 0 2px' }}>
            Nothing captured yet today — data appears here within ~5 minutes of the
            market being open with a valid token.
          </div>
        )}
      </div>
    </div>
  )
}
