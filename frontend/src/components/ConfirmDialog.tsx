import { useEffect, useRef } from 'react'

interface Props {
  message: string
  onConfirm: () => void
  onCancel: () => void
}

export default function ConfirmDialog({ message, onConfirm, onCancel }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => { ref.current?.focus() }, [])
  return (
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
      }}
      onClick={(e) => e.target === e.currentTarget && onCancel()}
    >
      <div
        ref={ref}
        tabIndex={-1}
        style={{ background: 'white', borderRadius: 8, padding: '24px 28px', maxWidth: 380, width: '90%' }}
      >
        <p style={{ marginBottom: 20, fontSize: 14, lineHeight: 1.5 }}>{message}</p>
        <div className="gap-8">
          <button className="btn btn-danger" onClick={onConfirm}>Confirm</button>
          <button className="btn btn-secondary" onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  )
}
