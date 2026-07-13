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

export default function Header({ title, showToast }) {
  const [ist, setIst] = useState(istNow())
  const [token] = useState({ state: 'ok', label: 'Token' })

  useEffect(() => {
    const tick = setInterval(() => setIst(istNow()), 1000)
    return () => clearInterval(tick)
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

  return (
    <header className="topbar">
      <h1 className="page-title">{title}</h1>
      <div className="topbar-chips">
        <span className="chip"><span className="num">{hhmmss(ist)}</span> IST</span>
        <span className="chip" title="NSE / BSE — equity & F&O, 09:15–15:30 IST">
          <span className={`dot ${eqOpen ? 'ok' : 'bad'}`}></span>
          Equity · {eqOpen ? 'Live' : 'Closed'}
        </span>
        <span className="chip" title="MCX — commodities, 09:00–23:30 IST">
          <span className={`dot ${mcxOpen ? 'ok' : 'bad'}`}></span>
          Commodity · {mcxOpen ? 'Live' : 'Closed'}
        </span>
        <span className="chip">
          <span className={`dot ${token.state}`}></span>
          <span>{token.label}</span>
          <button onClick={refreshToken} style={{ marginLeft: '4px' }}>refresh</button>
        </span>
        <span className="chip"><span className="dot ok"></span>Paper engine</span>
      </div>
    </header>
  )
}
