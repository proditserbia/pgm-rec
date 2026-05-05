import type { DiskUsageResponse } from '../types'

function fmtBytes(b: number) {
  if (b >= 1e12) return `${(b / 1e12).toFixed(1)} TB`
  if (b >= 1e9)  return `${(b / 1e9).toFixed(1)} GB`
  if (b >= 1e6)  return `${(b / 1e6).toFixed(0)} MB`
  return `${b} B`
}

export default function DiskWidget({ disk, error }: { disk: DiskUsageResponse | null; error?: string | null }) {
  if (error) return <span className="text-muted">Disk: unavailable</span>
  if (!disk)  return <span className="text-muted">Disk: loading…</span>
  const barCls =
    disk.percent_used >= 90 ? 'disk-crit' :
    disk.percent_used >= 80 ? 'disk-warn' : 'disk-ok'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      <span className="text-muted">Disk: <span style={{ fontFamily: 'monospace', fontSize: '0.85em' }}>{disk.path_checked}</span></span>
      <div className="disk-bar-outer">
        <div className={`disk-bar-inner ${barCls}`} style={{ width: `${disk.percent_used}%` }} />
      </div>
      <span className="text-muted" style={{ color: disk.percent_used >= 90 ? '#dc3545' : undefined }}>
        {disk.percent_used.toFixed(0)}% used
        &nbsp;({fmtBytes(disk.free_bytes)} free / {fmtBytes(disk.total_bytes)})
      </span>
      {disk.percent_used >= 90 && <span className="badge badge-error badge-pulse">⚠ DISK CRITICAL</span>}
      {disk.percent_used >= 80 && disk.percent_used < 90 && <span className="badge badge-degraded">⚠ DISK WARN</span>}
      {disk.warning && <span className="badge badge-degraded" title={disk.warning}>⚠ DISK PATH WARN</span>}
    </div>
  )
}
