// Reflection over a strategy's closed trades: headline stats, evidence-backed
// suggestions, and a by-exit-reason breakdown. Fed by result.insights
// (backtest) or GET /strategies/{id}/insights (paper) — same shape either way.
const inr = n => '₹' + Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })
const cell = { padding: '6px 10px', borderBottom: '1px solid var(--line)', fontFamily: 'var(--mono)', textAlign: 'left', fontSize: 12 }
const th = { ...cell, color: 'var(--muted)', fontWeight: 500 }
const pos = v => (v >= 0 ? 'var(--green)' : 'var(--red)')

export default function InsightsPanel({ insights }) {
  if (!insights || !insights.overall || !insights.overall.n) return null
  const o = insights.overall
  const suggestions = insights.suggestions || []
  const reasonRows = Object.entries(insights.by_reason || {}).sort((a, b) => b[1].n - a[1].n)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ fontSize: 13, color: 'var(--ink)', fontWeight: 600 }}>
        Reflection <span style={{ color: 'var(--muted)', fontWeight: 400, fontSize: 12 }}>(learn from closed trades)</span>
      </div>

      <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted)' }}>
        {o.n} closed · win {o.win_rate == null ? '—' : Math.round(o.win_rate * 100) + '%'} ·
        net <span style={{ color: pos(o.total) }}>{inr(o.total)}</span> ·
        expectancy <span style={{ color: pos(o.expectancy) }}>{inr(o.expectancy)}</span>/trade ·
        avg win {inr(o.avg_win)} / avg loss {inr(o.avg_loss)}
        {o.profit_factor != null && ` · PF ${o.profit_factor}`} · fees {inr(o.total_fees)}
      </div>

      {suggestions.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {suggestions.map((s, i) => (
            <div key={i} style={{ fontSize: 12, borderLeft: `3px solid ${s.rule === 'insufficient_data' ? 'var(--muted)' : 'var(--amber)'}`, paddingLeft: 8 }}>
              <div>{s.suggestion}</div>
              <div style={{ color: 'var(--muted)', fontFamily: 'var(--mono)', fontSize: 11 }}>{s.evidence}</div>
            </div>
          ))}
        </div>
      )}

      {reasonRows.length > 0 && (
        <div className="table-scroll">
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={th}>Exit reason</th><th style={th}>Trades</th>
                <th style={th}>Win rate</th><th style={th}>Total P&L</th>
                <th style={th}>Avg P&L</th>
                <th style={th}>Avg peak</th><th style={th}>Avg trough</th>
              </tr>
            </thead>
            <tbody>
              {reasonRows.map(([reason, b]) => (
                <tr key={reason}>
                  <td style={cell}>{reason}</td>
                  <td style={cell}>{b.n}</td>
                  <td style={cell}>{b.win_rate == null ? '—' : Math.round(b.win_rate * 100) + '%'}</td>
                  <td style={{ ...cell, color: pos(b.total) }}>{inr(b.total)}</td>
                  <td style={{ ...cell, color: pos(b.avg) }}>{inr(b.avg)}</td>
                  <td style={{ ...cell, color: 'var(--muted)' }}>{inr(b.avg_mfe)}</td>
                  <td style={{ ...cell, color: 'var(--muted)' }}>{inr(b.avg_mae)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
