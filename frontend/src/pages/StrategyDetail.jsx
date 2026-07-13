import { useState, useEffect } from 'react'
import WalkForward from '../components/WalkForward'
import BacktestPanel from '../components/BacktestPanel'
import PaperPanel from '../components/PaperPanel'

export default function StrategyDetail({ id, onBack, onDeploy, onLive, showToast }) {
  const [strategy, setStrategy] = useState(null)
  const [tab, setTab] = useState('paper')

  useEffect(() => {
    const load = async () => {
      try {
        const data = await fetch(`/strategies/${id}`).then(r => r.json())
        setStrategy(data)
      } catch (e) {
        console.error(e)
      }
    }
    load()
  }, [id])

  if (!strategy) return <div className="panel-body"><div className="empty">Loading...</div></div>

  return (
    /* No nested .panel — App already wraps views in one. */
    <div>
      <div className="panel-head">
        <button className="btn btn-ghost" onClick={onBack} title="Back to list">← Back</button>
        <h1>{strategy.name}</h1>
        <span className={`badge ${strategy.state}`}>{strategy.state.replace('DEPLOYED_', '')}</span>
        {['VALIDATED', 'STOPPED'].includes(strategy.state) && (
          <button className="btn btn-primary" onClick={() => onDeploy(id)} style={{ marginLeft: '8px' }}>Paper trade</button>
        )}
        <button className="btn btn-live" onClick={() => onLive(id)} style={{ marginLeft: '8px' }}>Trade live</button>
      </div>
      {(strategy.meta?.description || strategy.meta?.underlying) && (
        <div className="strat-desc">
          {strategy.meta?.description && <span>{strategy.meta.description}</span>}
          <span className="strat-facts">
            {[strategy.meta?.underlying,
              strategy.meta?.timeframe && `${strategy.meta.timeframe}-min bars`,
              strategy.meta?.segment].filter(Boolean).join(' · ')}
          </span>
        </div>
      )}
      <div className="tabs">
        {[['paper', 'Paper'], ['backtest', 'Backtest'], ['walkforward', 'Walk-Forward'], ['code', 'Code']].map(([t, label]) => (
          <button key={t} className={`tab ${tab === t ? 'on' : ''}`} onClick={() => setTab(t)}>
            {label}
          </button>
        ))}
      </div>
      <div className="panel-body">
        {tab === 'code' && (
          <div className="code-view">{strategy.code}</div>
        )}
        {tab === 'paper' && (
          <PaperPanel id={id} />
        )}
        {tab === 'backtest' && (
          <BacktestPanel id={id} underlying={strategy.meta?.underlying} />
        )}
        {tab === 'walkforward' && (
          <WalkForward id={id} params={strategy.meta?.params || {}} />
        )}
      </div>
    </div>
  )
}
