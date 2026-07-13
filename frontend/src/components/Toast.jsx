import { useEffect, useState } from 'react'

export default function Toast({ message }) {
  const [show, setShow] = useState(true)

  useEffect(() => {
    const timer = setTimeout(() => setShow(false), 2400)
    return () => clearTimeout(timer)
  }, [])

  return (
    <div className={`toast ${show ? 'show' : ''}`}>
      {message}
    </div>
  )
}
