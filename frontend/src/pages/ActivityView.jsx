import { useEffect, useState } from 'react'

export default function ActivityView() {
  const [events, setEvents] = useState([])
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10))

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
  }, [date])

  return (
    <div className="panel-body">
      <div style={{ marginBottom: '16px' }}>
        <label className="f" htmlFor="act-date">Date</label>
        <input
          type="date"
          id="act-date"
          value={date}
          onChange={e => setDate(e.target.value)}
          style={{ width: '160px' }}
        />
      </div>
      {!events.length ? (
        <div className="empty">No events on {date}.</div>
      ) : (
        <div className="tl">
          {events.map((e, i) => (
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
