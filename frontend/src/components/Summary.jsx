const fmt = n => '₹' + (n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })

export default function Summary({ alloc, equity, growth, live }) {
  return (
    <section className="summary" aria-label="Portfolio summary">
      <div>
        <div className="lbl">Capital allocated</div>
        <div className="val num">{fmt(alloc)}</div>
      </div>
      <div>
        <div className="lbl">Current equity</div>
        <div className="val num">{fmt(equity)}</div>
      </div>
      <div>
        <div className="lbl">Growth</div>
        <div className="val num" style={growth ? { color: growth > 0 ? 'var(--green)' : 'var(--red)' } : undefined}>{fmt(growth)}</div>
      </div>
      <div>
        <div className="lbl">Strategies live</div>
        <div className="val num">{live}</div>
      </div>
    </section>
  )
}
