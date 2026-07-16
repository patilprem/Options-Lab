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

export default function ScannerView({ showToast }) {
  const [data, setData] = useState(null)
  const [expanded, setExpanded] = useState(null)   // symbol whose detail is open
  const [detail, setDetail] = useState(null)

  const load = useCallback(async () => {
    try {
      const d = await fetch('/scanner').then(r => r.json())
      setData(d)
    } catch { showToast && showToast('Failed to load scanner') }
  }, [showToast])

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
