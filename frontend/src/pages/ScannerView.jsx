import { useState, useEffect, useCallback } from 'react'

const cell = { padding: '6px 10px', borderBottom: '1px solid var(--line)', fontFamily: 'var(--mono)', textAlign: 'left', fontSize: 13 }
const th = { ...cell, color: 'var(--muted)', fontWeight: 500 }
const num = { ...cell, textAlign: 'right' }

const pct = n => (n == null ? '—' : (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%')
const BUILDUP_LABEL = {
  long_buildup: 'Long buildup', short_buildup: 'Short buildup',
  short_covering: 'Short covering', long_unwinding: 'Long unwinding',
  neutral: 'Neutral', unknown: '—',
}

function ScoreBadge({ score }) {
  const s = score || 0
  const color = s >= 70 ? 'var(--green)' : s >= 45 ? 'var(--amber)' : 'var(--muted)'
  return (
    <span style={{ fontFamily: 'var(--mono)', fontWeight: 700, color }}>{s.toFixed(0)}</span>
  )
}

function BiasTag({ bias }) {
  if (!bias) return <span style={{ color: 'var(--muted)' }}>—</span>
  const color = bias === 'CE' ? 'var(--green)' : 'var(--red)'
  return (
    <span style={{ color, border: `1px solid ${color}`, borderRadius: 4, padding: '1px 6px', fontSize: 12, fontWeight: 700 }}>
      {bias === 'CE' ? 'CALL' : 'PUT'}
    </span>
  )
}

function BiasCard({ name, bias }) {
  const cur = bias?.current
  const score = cur?.score
  const acc = (bias?.accuracy || [])[0]
  const color = score == null ? 'var(--muted)' : score > 0.3 ? 'var(--green)' : score < -0.3 ? 'var(--red)' : 'var(--amber)'
  const label = score == null ? 'no read' : score > 0.3 ? 'BULLISH' : score < -0.3 ? 'BEARISH' : 'NEUTRAL'
  const pos = score == null ? 50 : (score + 1) / 2 * 100   // -1..1 -> 0..100
  return (
    <div style={{ flex: '1 1 240px', border: '1px solid var(--line)', borderRadius: 8, padding: '12px 14px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <b>{name}</b>
        <span style={{ color, fontWeight: 700, fontSize: 13 }}>{label}</span>
      </div>
      {/* bias meter: bearish (left) .. bullish (right) */}
      <div style={{ position: 'relative', height: 6, background: 'var(--bg)', border: '1px solid var(--line)', borderRadius: 4, margin: '10px 0 6px' }}>
        <div style={{ position: 'absolute', left: '50%', top: -2, bottom: -2, width: 1, background: 'var(--line)' }} />
        <div style={{ position: 'absolute', left: `calc(${pos}% - 4px)`, top: -3, width: 8, height: 10, borderRadius: 2, background: color }} />
      </div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted)' }}>
        score {score == null ? '—' : score.toFixed(2)} · {cur?.n || 0} members · cov {cur?.coverage?.toFixed(0) ?? '—'}%
      </div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
        {acc ? `accuracy ${Math.round((acc.hit_rate || 0) * 100)}% (${acc.hits}/${acc.n} @ ${acc.horizon_min}m)` : 'accuracy: not yet scored'}
      </div>
    </div>
  )
}

export default function ScannerView({ showToast }) {
  const [data, setData] = useState(null)
  const [bias, setBias] = useState(null)
  const [expanded, setExpanded] = useState(null)   // symbol whose detail is open
  const [detail, setDetail] = useState(null)

  const [valid, setValid] = useState(null)
  const [book, setBook] = useState(null)

  const load = useCallback(async () => {
    try {
      const [d, b, v, tb] = await Promise.all([
        fetch('/scanner').then(r => r.json()),
        fetch('/scanner/index-bias').then(r => r.json()).catch(() => null),
        fetch('/scanner/validation').then(r => r.json()).catch(() => null),
        fetch('/scanner/trades').then(r => r.json()).catch(() => null),
      ])
      setData(d)
      setBias(b)
      setValid(v)
      setBook(tb)
    } catch { showToast && showToast('Failed to load scanner') }
  }, [showToast])

  const setTrading = async (on) => {
    await fetch('/scanner/trade-settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: on }),
    })
    showToast && showToast(`Auto-trading ${on ? 'ON (paper)' : 'off'}`)
    load()
  }

  useEffect(() => {
    load()
    const iv = setInterval(load, 15000)
    return () => clearInterval(iv)
  }, [load])

  const toggle = async (sym) => {
    if (expanded === sym) { setExpanded(null); setDetail(null); return }
    setExpanded(sym)
    try { setDetail(await fetch(`/scanner/${sym}`).then(r => r.json())) }
    catch { setDetail(null) }
  }

  const setEnabled = async (on) => {
    await fetch('/scanner/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: on }),
    })
    showToast && showToast(`Scanner ${on ? 'enabled' : 'disabled'}`)
    load()
  }

  if (!data) return <div className="empty">Loading scanner…</div>
  const scores = data.scores || []

  return (
    <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* status bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 9, height: 9, borderRadius: '50%', background: data.enabled ? 'var(--green)' : 'var(--muted)' }} />
          <b>{data.enabled ? 'Scanning' : 'Off'}</b>
        </span>
        <span style={{ color: 'var(--muted)', fontSize: 13 }}>
          {data.universe_size} FNO stocks · {data.session ? 'market open' : 'market closed'}
          {data.last_sweep ? ` · last sweep ${String(data.last_sweep).slice(11, 19)}` : ''}
        </span>
        <span style={{ color: 'var(--muted)', fontSize: 13 }}>alert ≥ {data.alert_score}</span>
        <button className="btn" style={{ marginLeft: 'auto' }} onClick={() => setEnabled(!data.enabled)}>
          {data.enabled ? 'Turn off' : 'Turn on'}
        </button>
      </div>

      {!data.enabled && (
        <div style={{ background: 'var(--amber-tint, rgba(200,150,0,.08))', border: '1px solid var(--line)', color: 'var(--muted)', padding: '10px 12px', borderRadius: 8, fontSize: 13 }}>
          The scanner needs live Dhan credentials and runs only during market hours. Turn it on to begin the whole-universe sweep; Tier-2 chain deep-dives follow on the shortlist.
        </div>
      )}

      {/* index bias cards */}
      {bias && (
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          {Object.keys(bias).map(name => <BiasCard key={name} name={name} bias={bias[name]} />)}
        </div>
      )}

      {/* validation: measured forward-return hit-rate of flagged setups */}
      {valid?.overall?.n > 0 && (
        <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted)', border: '1px solid var(--line)', borderRadius: 8, padding: '8px 12px' }}>
          <b style={{ color: 'var(--ink)' }}>Validation</b> · {valid.overall.n} flagged setups @ {valid.horizon_min}min ·
          hit-rate {valid.overall.hit_rate == null ? '—' : Math.round(valid.overall.hit_rate * 100) + '%'} ·
          avg {valid.overall.avg_return_pct == null ? '—' : valid.overall.avg_return_pct + '%'}
          {valid.by_score?.['70-100']?.n > 0 && (
            <span> · high-score band {Math.round((valid.by_score['70-100'].hit_rate || 0) * 100)}% ({valid.by_score['70-100'].n})</span>
          )}
        </div>
      )}

      {/* positional paper trading book */}
      {book && (
        <div style={{ border: '1px solid var(--line)', borderRadius: 8, padding: '10px 12px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <b>Auto-trader <span style={{ color: 'var(--muted)', fontWeight: 400, fontSize: 12 }}>(paper)</span></b>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: book.enabled ? 'var(--green)' : 'var(--muted)' }} />
              {book.enabled ? 'trading' : 'off'}
            </span>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted)' }}>
              {book.open}/{book.max_positions} open · realized ₹{Math.round(book.realized).toLocaleString('en-IN')} ·
              unrealized <span style={{ color: book.unrealized >= 0 ? 'var(--green)' : 'var(--red)' }}>₹{Math.round(book.unrealized).toLocaleString('en-IN')}</span>
            </span>
            <button className="btn" style={{ marginLeft: 'auto' }} onClick={() => setTrading(!book.enabled)}>
              {book.enabled ? 'Stop' : 'Start (paper)'}
            </button>
          </div>
          {book.positions?.length > 0 && (
            <div style={{ overflowX: 'auto', marginTop: 8 }}>
              <table style={{ borderCollapse: 'collapse', width: '100%', minWidth: 560 }}>
                <thead>
                  <tr>
                    <th style={th}>Symbol</th><th style={th}>Side</th>
                    <th style={{ ...th, textAlign: 'right' }}>Lots</th>
                    <th style={{ ...th, textAlign: 'right' }}>Entry</th>
                    <th style={{ ...th, textAlign: 'right' }}>Mark</th>
                    <th style={{ ...th, textAlign: 'right' }}>Stop</th>
                    <th style={{ ...th, textAlign: 'right' }}>P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {book.positions.map(p => (
                    <tr key={p.symbol}>
                      <td style={{ ...cell, fontWeight: 700 }}>{p.symbol}</td>
                      <td style={cell}><BiasTag bias={p.bias} /></td>
                      <td style={num}>{p.lots}</td>
                      <td style={num}>{p.entry?.toFixed(2)}</td>
                      <td style={num}>{p.mtm?.toFixed(2)}</td>
                      <td style={{ ...num, color: 'var(--muted)' }}>{p.stop?.toFixed(2)}</td>
                      <td style={{ ...num, color: p.unrealized >= 0 ? 'var(--green)' : 'var(--red)' }}>
                        {p.unrealized >= 0 ? '+' : ''}{Math.round(p.unrealized).toLocaleString('en-IN')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {scores.length === 0 ? (
        <div className="empty">No setups yet — the scanner populates during market hours.</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ borderCollapse: 'collapse', width: '100%', minWidth: 640 }}>
            <thead>
              <tr>
                <th style={th}>Symbol</th>
                <th style={{ ...th, textAlign: 'right' }}>Score</th>
                <th style={th}>Bias</th>
                <th style={th}>Buildup</th>
                <th style={{ ...th, textAlign: 'right' }}>Δ price</th>
                <th style={{ ...th, textAlign: 'right' }}>Vol×</th>
                <th style={th}>Why</th>
              </tr>
            </thead>
            <tbody>
              {scores.map(s => (
                <FragmentRow key={s.symbol} s={s} expanded={expanded === s.symbol}
                  detail={expanded === s.symbol ? detail : null} onToggle={() => toggle(s.symbol)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function FragmentRow({ s, expanded, detail, onToggle }) {
  return (
    <>
      <tr onClick={onToggle} style={{ cursor: 'pointer', background: expanded ? 'var(--bg)' : 'transparent' }}>
        <td style={{ ...cell, fontWeight: 700 }}>{s.symbol}{!s.deep_dived && <span style={{ color: 'var(--muted)', fontWeight: 400 }}> ·t1</span>}</td>
        <td style={num}><ScoreBadge score={s.score} /></td>
        <td style={cell}><BiasTag bias={s.bias} /></td>
        <td style={cell}>{BUILDUP_LABEL[s.buildup] || '—'}</td>
        <td style={{ ...num, color: (s.price_change_pct || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>{pct(s.price_change_pct)}</td>
        <td style={num}>{s.volume_surge ? s.volume_surge.toFixed(1) : '—'}</td>
        <td style={{ ...cell, color: 'var(--muted)', fontSize: 12 }}>{(s.reasons || []).slice(0, 2).join(' · ')}</td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={7} style={{ ...cell, background: 'var(--bg)' }}>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18, padding: '4px 2px' }}>
              <div>
                <div style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 4 }}>Reasons</div>
                <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12 }}>
                  {(s.reasons || []).map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              </div>
              {detail?.tier2 && (
                <div style={{ fontSize: 12 }}>
                  <div style={{ color: 'var(--muted)', marginBottom: 4 }}>Chain (Tier-2)</div>
                  <div>PCR(OI): {detail.tier2.pcr_oi?.toFixed(2) ?? '—'} · ATM IV: {detail.tier2.atm_iv?.toFixed(1) ?? '—'} · skew: {detail.tier2.iv_skew?.toFixed(1) ?? '—'}</div>
                  <div>Liquidity: {detail.tier2.liquidity?.ok ? 'ok' : (detail.tier2.liquidity?.reason || 'n/a')}</div>
                </div>
              )}
              {detail?.universe && (
                <div style={{ fontSize: 12 }}>
                  <div style={{ color: 'var(--muted)', marginBottom: 4 }}>Contract</div>
                  <div>lot {detail.universe.lot_size} · exp {detail.universe.near_expiry}</div>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}
