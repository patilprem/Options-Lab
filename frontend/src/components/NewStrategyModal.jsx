import { useState } from 'react'
import Modal from './Modal'

export default function NewStrategyModal({ open, onClose, onSave, showToast }) {
  const [name, setName] = useState('')
  const [code, setCode] = useState('')
  const [errors, setErrors] = useState([])

  const handleSave = async () => {
    setErrors([])
    try {
      await onSave(name, code)
      setName('')
      setCode('')
    } catch (e) {
      const err = e instanceof Error ? e.message : JSON.stringify(e)
      setErrors([err])
    }
  }

  return (
    <Modal open={open} onClose={onClose} className="modal-wide">
      <div className="modal-head">
        <h3>New strategy</h3>
      </div>
      <div className="modal-body">
        <label className="f" htmlFor="ns-name">Strategy name</label>
        <input
          type="text"
          id="ns-name"
          placeholder="Short straddle 9:20"
          value={name}
          onChange={e => setName(e.target.value)}
        />
        <div style={{ height: '12px' }}></div>
        <label className="f" htmlFor="ns-code">Paste strategy code</label>
        <textarea
          id="ns-code"
          placeholder="class MyStrategy(Strategy): ..."
          spellCheck="false"
          value={code}
          onChange={e => setCode(e.target.value)}
        ></textarea>
        <p className="hint">Generate the code with your LLM using <span className="num">prompts/strategy_prompt.md</span>.</p>
        {errors.length > 0 && (
          <div className="errors">{errors.join('\n\n')}</div>
        )}
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" onClick={handleSave}>Validate &amp; add</button>
      </div>
    </Modal>
  )
}
