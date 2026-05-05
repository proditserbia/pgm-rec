import type { WatchdogEventResponse } from '../../types'
import { TZ, fmtDate } from '../../utils/format'

interface Props {
  events: WatchdogEventResponse[]
}

export default function WatchdogEventsCard({ events }: Props) {
  return (
    <div className="card">
      <div className="card-title">Recent Watchdog Events</div>
      {events.length === 0
        ? <p className="empty-state">No watchdog events recorded.</p>
        : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time ({TZ})</th>
                  <th>Event</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {events.map(e => (
                  <tr key={e.id}>
                    <td>{fmtDate(e.detected_at)}</td>
                    <td><span className="badge badge-orange">{e.event_type}</span></td>
                    <td className="text-muted">{e.details ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </div>
  )
}
