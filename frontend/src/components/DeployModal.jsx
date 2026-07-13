import { useState } from 'react'
import Modal from './Modal'

export default function DeployModal({ open, onClose, onDeploy, showToast }) {
  const [capital, setCapital] = useState(500000)
  const [sqOff, setSqOff] = useState(false)
  const [start, setStart] = useState(true)

  const handleDeploy = async () => {
    try {
      await onDeploy(capital, sqOff, start)
    } catch (e) {
      showToast(typeof e === 'string' ? e : 'Deploy failed')
    }
  }

  return (
    <Modal open={open} onClose={onClose}>
      <div className="modal-head">
        <h3>Deploy for paper trading</h3>
      </div>
      <div className="modal-body">
        <div className="form-grid" style={{ gridTemplateColumns: '1fr' }}>
          <div>
            <label className="f" htmlFor="dm-cap">Capital to allocate (₹)</label>
            <input
              type="number"
              id="dm-cap"
              min="0"
              step="10000"
              value={capital}
              onChange={e => setCapital(+e.target.value)}
            />
          </div>
        </div>
        <label className="switch">
          <input type="checkbox" checked={sqOff} onChange={e => setSqOff(e.target.checked)} />
          Square off open positions when paused
        </label>
        <label className="switch">
          <input type="checkbox" checked={start} onChange={e => setStart(e.target.checked)} />
          Start taking entries immediately
        </label>
        <p className="hint">Entries are rejected if margin exceeds unused capital.</p>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" onClick={handleDeploy}>Deploy paper</button>
      </div>
    </Modal>
  )
}
