import type { ChannelStatusResponse } from '../../types'
import { TZ, fmtDate, fmtUptime } from '../../utils/format'

interface Props {
  status: ChannelStatusResponse
}

export default function RecordingStatusCard({ status }: Props) {
  return (
    <div className="card">
      <div className="card-title">Recording Status</div>
      <div className="card-row">
        <span className="card-label">PID</span>
        <span className="card-value">{status.pid ?? '—'}</span>
      </div>
      <div className="card-row">
        <span className="card-label">Uptime</span>
        <span className="card-value">{fmtUptime(status.uptime_seconds)}</span>
      </div>
      <div className="card-row">
        <span className="card-label">Last seen alive</span>
        <span className="card-value">
          {fmtDate(status.last_seen_alive)}
          <span className="tz-label">{TZ}</span>
        </span>
      </div>
      <div className="card-row">
        <span className="card-label">Started at</span>
        <span className="card-value">
          {fmtDate(status.started_at)}
          <span className="tz-label">{TZ}</span>
        </span>
      </div>
    </div>
  )
}
