import { useState, useEffect } from 'react'

// IST = UTC+5:30 (no DST). Shift the epoch by +5:30 and read the UTC fields to
// get IST wall-clock regardless of the viewer's own timezone.
const istNow = () => new Date(Date.now() + 5.5 * 3600 * 1000)
const hhmmss = ist => ist.toISOString().slice(11, 19)

// Regular exchange sessions (IST minutes-from-midnight, Mon–Fri).
// NOTE: schedule-based — does not account for exchange holidays / special sessions.
const EQUITY = [9 * 60 + 15, 15 * 60 + 30]   // NSE/BSE cash + F&O
const MCX = [9 * 60, 23 * 60 + 30]           // commodities (evening session)

function open(ist, [start, end]) {
  const day = ist.getUTCDay()                 // 0 Sun … 6 Sat (UTC fields = IST here)
  if (day === 0 || day === 6) return false
  const mins = ist.getUTCHours() * 60 + ist.getUTCMinutes()
  return mins >= start && mins <= end
}

// Feed pill: socket state + tick freshness, softened outside market hours
// (a quiet or dropped socket at midnight is normal, not an incident).
function feedPill(feed, anyMarketOpen) {
  if (!feed) return { dot: 'off', label: 'Feed · …' }
  if (feed.mode === 'off') return { dot: 'off', label: 'Feed · Off' }
  if (feed.mode === 'synthetic') return { dot: 'warn', label: 'Feed · Synthetic' }
  if (!anyMarketOpen) return { dot: 'off', label: 'Feed · Off-hours' }
  if (!feed.connected) return { dot: 'bad', label: 'Feed · Down' }
  if (feed.tick_age_sec != null && feed.tick_age_sec < 120)
    return { dot: 'ok', label: 'Feed · Live' }
  return { dot: 'warn', label: 'Feed · Quiet' }
}

function tokenDot(token) {
  if (!token) return 'off'
  return token.state === 'ok' ? 'ok' : token.state === 'expiring' ? 'warn' : 'bad'
}

export default function Header({ title, showToast }) {
  const [ist, setIst] = useState(istNow())
  const [feed, setFeed] = useState(null)
  const [token, setToken] = useState(null)
  const [live, setLive] = useState(null)

  useEffect(() => {
    const tick = setInterval(() => setIst(istNow()), 1000)
    return () => clearInterval(tick)
  }, [])

  useEffect(() => {
    const poll = async () => {
      try {
        const [f, t, l] = await Promise.all([
          fetch('/feed/status').then(r => r.json()),
          fetch('/token/status').then(r => r.json()),
          fetch('/live/status').then(r => r.json()),
        ])
        setFeed(f); setToken(t); setLive(l)
      } catch (e) { /* transient (restart window) — keep last known state */ }
    }
    poll()
    const iv = setInterval(poll, 10000)
    return () => clearInterval(iv)
  }, [])

  const refreshToken = async () => {
    try {
      const r = await fetch('/token/refresh', { method: 'POST' })
      if (r.ok) showToast('Login link sent to your phone')
    } catch (e) {
      showToast('Token refresh failed')
    }
  }

  const eqOpen = open(ist, EQUITY)
  const mcxOpen = open(ist, MCX)
  // judge the feed only against sessions it actually subscribes to
  const segs = feed?.segments || ['NSE', 'MCX']
  const feedMarketOpen = (segs.includes('NSE') && eqOpen)
    || (segs.includes('MCX') && mcxOpen)
  const fp = feedPill(feed, feedMarketOpen)

  return (
    <header className="topbar">
      <h1 className="page-title">{title}</h1>
      <div className="topbar-chips">
        <span className="chip"><span className="num">{hhmmss(ist)}</span> IST</span>

        <span className="chip-group">
          <span className="chip-group-label">Market</span>
          <span className="chip" title="NSE / BSE — equity & F&O, 09:15–15:30 IST">
            <span className={`dot ${eqOpen ? 'ok' : 'off'}`}></span>
            Equity · {eqOpen ? 'Live' : 'Closed'}
          </span>
          <span className="chip" title="MCX — commodities, 09:00–23:30 IST">
            <span className={`dot ${mcxOpen ? 'ok' : 'off'}`}></span>
            Commodity · {mcxOpen ? 'Live' : 'Closed'}
          </span>
        </span>

        <span className="chip-group">
          <span className="chip-group-label">Data</span>
          <span className="chip" title={feed?.last_tick
            ? `last tick ${feed.last_tick} (${feed.tick_age_sec}s ago)`
            : 'market-data websocket'}>
            <span className={`dot ${fp.dot}`}></span>{fp.label}
          </span>
          <span className="chip" title={token?.expires_at
            ? `Dhan token · ${token.hours_left}h left` : 'Dhan token'}>
            <span className={`dot ${tokenDot(token)}`}></span>
            <span>Token</span>
            <button onClick={refreshToken} style={{ marginLeft: '4px' }}>refresh</button>
          </span>
        </span>

        <span className="chip-group">
          <span className="chip-group-label">Engine</span>
          <span className="chip"><span className="dot ok"></span>Paper</span>
          <span className="chip" title="real-order path (gated)">
            <span className={`dot ${live?.enabled ? (live?.dry_run ? 'warn' : 'ok') : 'off'}`}></span>
            Live · {live?.enabled ? (live?.dry_run ? 'Dry-run' : 'Armed') : 'Off'}
          </span>
        </span>
      </div>
    </header>
  )
}
