import { useEffect, useState } from 'react'

// What a trader needs to see by default: money events (fills/stops/blocks),
// risk actions, deploy/pause lifecycle, token state, and anything warn/error.
// Feed/data chatter stays behind the Feed/All chips.
const FILTERS = {
  Important: e => ['fill', 'stop_loss', 'block', 'risk', 'lifecycle', 'token'].includes(e.kind)
    || e.level === 'warn' || e.level === 'error',
  Fills: e => ['fill', 'stop_loss', 'block'].includes(e.kind),
  Errors: e => e.level === 'warn' || e.level === 'error',
  Feed: e => e.kind === 'feed' || e.kind === 'data',
  All: () => true,
}

export default function ActivityView() {
  const [events, setEvents] = useState([])
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10))
  const [filter, setFilter] = useState('Important')

  useEffect(() => {
    const load = async () => {
      try {
        const data = await fetch(`/activity?date=${date}`).then(r => r.json())
        setEvents(data.events || [])
      } catch (e) {
        console.error(e)
      }
    }
    load()
    const iv = setInterval(load, 15000)   // keep "latest" actually latest
    return () => clearInterval(iv)
  }, [date])

  // newest first regardless of what the API returns
  const shown = events
    .slice()
    .sort((a, b) => (b.ts || '').localeCompare(a.ts || ''))
    .filter(FILTERS[filter])

  return (
    <div className="panel-body">
      <div style={{ display: 'flex', gap: '12px', alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: '16px' }}>
        <div>
          <label className="f" htmlFor="act-date">Date</label>
          <input
            type="date"
            id="act-date"
            value={date}
            onChange={e => setDate(e.target.value)}
            style={{ width: '160px' }}
          />
        </div>
        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
          {Object.keys(FILTERS).map(k => (
            <button
              key={k}
              onClick={() => setFilter(k)}
              className="badge"
              style={{
                cursor: 'pointer', padding: '6px 12px', fontSize: '11px',
                background: filter === k ? 'var(--green-dim, rgba(120,220,120,.15))' : 'transparent',
                color: filter === k ? 'var(--green)' : 'var(--muted)',
                border: `1px solid ${filter === k ? 'var(--green)' : 'var(--line)'}`,
                borderRadius: '999px',
              }}
            >
              {k}
            </button>
          ))}
        </div>
      </div>
      {!shown.length ? (
        <div className="empty">
          {events.length
            ? `No ${filter.toLowerCase()} events on ${date} — try All.`
            : `No events on ${date}.`}
        </div>
      ) : (
        <div className="tl">
          {shown.map((e, i) => (
            <div key={i} className={`tl-item ${e.level}`}>
              <span className="tl-time">{(e.ts || '').slice(11, 19)}</span>
              <span className="tl-kind">{e.kind}</span>
              {e.strategy && <span className="tl-strat">{e.strategy}</span>}
              <span className="tl-msg">{e.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
