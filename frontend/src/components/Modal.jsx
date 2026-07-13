import { useRef, useEffect } from 'react'

/**
 * Native <dialog> opened via showModal() — so it centers in the viewport top
 * layer and gets a real ::backdrop (blur + dim). Using the `open` *attribute*
 * (as the old modals did) renders a NON-modal dialog: top-left, no backdrop.
 * Also handles Esc and backdrop-click to close.
 */
export default function Modal({ open, onClose, className = '', children }) {
  const ref = useRef(null)

  useEffect(() => {
    const d = ref.current
    if (!d) return
    if (open && !d.open) d.showModal()
    else if (!open && d.open) d.close()
  }, [open])

  useEffect(() => {
    const d = ref.current
    if (!d) return
    const onCancel = (e) => { e.preventDefault(); onClose && onClose() }  // Esc
    d.addEventListener('cancel', onCancel)
    return () => d.removeEventListener('cancel', onCancel)
  }, [onClose])

  // A click whose target is the dialog element itself = a click on the backdrop.
  const onClick = (e) => { if (e.target === ref.current) onClose && onClose() }

  return (
    <dialog ref={ref} className={`modal ${className}`} onClick={onClick}>
      {children}
    </dialog>
  )
}
