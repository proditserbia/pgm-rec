export default function ProgressBar({ percent, status }: { percent: number; status?: string }) {
  const cls =
    status === 'completed' ? 'done' :
    status === 'failed' || status === 'cancelled' ? 'failed' : ''
  return (
    <div className="progress-outer">
      <div
        className={`progress-inner ${cls}`}
        style={{ width: `${Math.min(100, Math.max(0, percent))}%` }}
      />
    </div>
  )
}
