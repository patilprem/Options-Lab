import { useState, useEffect } from 'react'
import Modal from './Modal'

const ITEMS = [
  '20+ paper sessions completed',
  'Profitable after fees over the paper period',
  'Max drawdown within your written limit',
  'Reality gap acceptable vs backtest',
  'Kill-switch tested, daily loss cap set',
  'Starting at minimum lot size',
]

export default function LiveModal({ open, id, onClose, showToast }) {
  const [checked, setChecked] = useState(ITEMS.map(() => false))
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open) {
      setChecked(ITEMS.map(() => false))
      fetch('/live/status').then(r => r.json()).then(setStatus).catch(() => setStatus(null))
    }
  }, [open, id])

  const allChecked = checked.every(Boolean)
  const canDeploy = allChecked && status?.enabled && id

  const deploy = async () => {
    setBusy(true)
    try {
      await fetch(`/strategies/${id}/live/ack`, { method: 'POST' })
      const res = await fetch(`/strategies/${id}/deploy_live`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'deploy failed')
      showToast && showToast(data.dry_run ? 'Live deployed (DRY-RUN, paused)' : 'LIVE deployed (paused)')
      onClose()
    } catch (e) {
      showToast && showToast(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose}>
      <div className="modal-head">
        <h3>Trade live — real money</h3>
      </div>
      <div className="modal-body">
        {status && (
          <div className={`banner ${status.enabled ? (status.dry_run ? 'warn' : 'live') : 'warn'}`}>
            {!status.enabled
              ? 'Live trading is OFF. Enable it in Live settings (server) before deploying.'
              : status.dry_run
                ? 'DRY-RUN mode: orders are logged, not sent. Real orders also require the whitelisted static IP.'
                : '⚠ REAL ORDERS ENABLED — orders will be sent to Dhan.'}
          </div>
        )}
        <ul className="check-list" style={{ listStyle: 'none', padding: 0 }}>
          {ITEMS.map((item, i) => (
            <li key={i} style={{ padding: '4px 0' }}>
              <label style={{ display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer' }}>
                <input type="checkbox" checked={checked[i]}
                  onChange={e => setChecked(c => c.map((v, j) => (j === i ? e.target.checked : v)))} />
                {item}
              </label>
            </li>
          ))}
        </ul>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost" onClick={onClose}>Close</button>
        <button className="btn btn-live" disabled={!canDeploy || busy} onClick={deploy}>
          {busy ? 'Deploying…' : status?.dry_run ? 'Deploy live (dry-run)' : 'Deploy live'}
        </button>
      </div>
    </Modal>
  )
}
