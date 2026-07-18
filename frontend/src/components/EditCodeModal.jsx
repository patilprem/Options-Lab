import { useEffect, useState } from 'react'
import Modal from './Modal'

export default function EditCodeModal({ open, code, onClose, onSave }) {
  const [draft, setDraft] = useState(code)
  const [errors, setErrors] = useState([])
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (open) { setDraft(code); setErrors([]) }
  }, [open, code])

  const handleSave = async () => {
    setErrors([])
    setSaving(true)
    try {
      await onSave(draft)
    } catch (e) {
      setErrors(e.errors || [e.message || 'Request failed'])
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} className="modal-wide">
      <div className="modal-head">
        <h3>Edit code</h3>
      </div>
      <div className="modal-body">
        <label className="f" htmlFor="ec-code">Strategy code</label>
        <textarea
          id="ec-code"
          spellCheck="false"
          value={draft}
          onChange={e => setDraft(e.target.value)}
        ></textarea>
        <p className="hint">Saving re-validates the code and moves the strategy back to VALIDATED. Blocked while running or deployed — pause/stop it first.</p>
        {errors.length > 0 && (
          <div className="errors">{errors.join('\n\n')}</div>
        )}
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost" onClick={onClose} disabled={saving}>Cancel</button>
        <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
          {saving ? 'Validating…' : 'Validate & save'}
        </button>
      </div>
    </Modal>
  )
}
