import { useState, useEffect } from 'react'
import WalkForward from '../components/WalkForward'
import BacktestPanel from '../components/BacktestPanel'
import PaperPanel from '../components/PaperPanel'
import EditCodeModal from '../components/EditCodeModal'

const DEPLOYED_STATES = ['RUNNING', 'DEPLOYED_PAUSED']

export default function StrategyDetail({ id, onBack, onDeploy, onLive, onChanged, showToast }) {
  const [strategy, setStrategy] = useState(null)
  const [tab, setTab] = useState('paper')
  const [renaming, setRenaming] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const [editingCode, setEditingCode] = useState(false)
  const [isLive, setIsLive] = useState(false)
  const [ctrlBusy, setCtrlBusy] = useState(false)

  const load = async () => {
    try {
      const data = await fetch(`/strategies/${id}`).then(r => r.json())
      setStrategy(data)
    } catch (e) {
      console.error(e)
    }
    try {
      const live = await fetch('/live/status').then(r => r.json())
      setIsLive((live.deployed || []).includes(id))
    } catch (e) { /* live status is a bonus for picking the right control path */ }
  }

  useEffect(() => { load() }, [id])

  if (!strategy) return <div className="panel-body"><div className="empty">Loading...</div></div>

  const deployed = DEPLOYED_STATES.includes(strategy.state)

  const control = async (cmd) => {
    setCtrlBusy(true)
    try {
      const path = isLive ? `/strategies/${id}/live/${cmd}` : `/strategies/${id}/${cmd}`
      const res = await fetch(path, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || `${cmd} failed`)
      showToast?.(`${cmd === 'play' ? 'Running ▶' : cmd === 'pause' ? 'Paused' : 'Stopped'}`)
      await load()
      onChanged?.()
    } catch (e) {
      showToast?.(e.message)
    } finally {
      setCtrlBusy(false)
    }
  }

  const startRename = () => { setNameDraft(strategy.name); setRenaming(true) }

  const saveRename = async () => {
    const name = nameDraft.trim()
    setRenaming(false)
    if (!name || name === strategy.name) return
    try {
      const res = await fetch(`/strategies/${id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Rename failed')
      showToast?.('Renamed ✓')
      await load()
      onChanged?.()
    } catch (e) {
      showToast?.(e.message)
    }
  }

  const saveCode = async (code) => {
    const res = await fetch(`/strategies/${id}/code`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    })
    const data = await res.json()
    if (!res.ok) {
      const detail = data.detail
      if (detail && typeof detail === 'object') {
        const err = new Error('Validation failed')
        err.errors = [...(detail.errors || []), ...(detail.warnings || [])]
        throw err
      }
      throw new Error(detail || 'Save failed')
    }
    setEditingCode(false)
    showToast?.('Code updated & revalidated ✓')
    await load()
    onChanged?.()
  }

  const handleDelete = async () => {
    if (!window.confirm(`Delete "${strategy.name}"? This removes its backtests, trades and history too.`)) return
    try {
      const res = await fetch(`/strategies/${id}`, { method: 'DELETE' })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Delete failed')
      showToast?.('Strategy deleted')
      onBack()
      onChanged?.()
    } catch (e) {
      showToast?.(e.message)
    }
  }

  return (
    /* No nested .panel — App already wraps views in one. */
    <div>
      <div className="panel-head">
        <button className="btn btn-ghost" onClick={onBack} title="Back to list">← Back</button>
        {renaming ? (
          <input
            autoFocus
            value={nameDraft}
            onChange={e => setNameDraft(e.target.value)}
            onBlur={saveRename}
            onKeyDown={e => {
              if (e.key === 'Enter') e.currentTarget.blur()
              if (e.key === 'Escape') setRenaming(false)
            }}
            style={{ font: 'inherit', maxWidth: 320 }}
          />
        ) : (
          <h1 onClick={startRename} title="Click to rename" style={{ cursor: 'text' }}>{strategy.name}</h1>
        )}
        <span className={`badge ${strategy.state}`}>{strategy.state.replace('DEPLOYED_', '')}</span>
        {['VALIDATED', 'STOPPED'].includes(strategy.state) && (
          <button className="btn btn-primary" onClick={() => onDeploy(id)} style={{ marginLeft: '8px' }}>Paper trade</button>
        )}
        {strategy.state === 'RUNNING' && (
          <button className="btn btn-ghost" onClick={() => control('pause')} disabled={ctrlBusy} style={{ marginLeft: '8px' }}>Pause</button>
        )}
        {strategy.state === 'DEPLOYED_PAUSED' && (
          <button className="btn btn-primary" onClick={() => control('play')} disabled={ctrlBusy} style={{ marginLeft: '8px' }}>Play</button>
        )}
        {deployed && (
          <button className="btn btn-ghost" onClick={() => control('stop')} disabled={ctrlBusy} style={{ marginLeft: '8px' }}>Stop</button>
        )}
        <button className="btn btn-live" onClick={() => onLive(id)} style={{ marginLeft: '8px' }}>Trade live</button>
        <button
          className="btn btn-danger"
          onClick={handleDelete}
          disabled={deployed}
          title={deployed ? 'Stop the strategy before deleting it' : 'Delete strategy'}
          style={{ marginLeft: '8px' }}
        >Delete</button>
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
          <>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10 }}>
              <button
                className="btn btn-ghost"
                onClick={() => setEditingCode(true)}
                disabled={deployed}
                title={deployed ? 'Stop the strategy before editing its code' : 'Edit code'}
              >Edit code</button>
            </div>
            <div className="code-view">{strategy.code}</div>
          </>
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
      <EditCodeModal
        open={editingCode}
        code={strategy.code}
        onClose={() => setEditingCode(false)}
        onSave={saveCode}
      />
    </div>
  )
}
