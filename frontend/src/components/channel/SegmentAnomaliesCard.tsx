import type { SegmentAnomalyResponse } from '../../types'
import { TZ, fmtDate } from '../../utils/format'

interface Props {
  anomalies: SegmentAnomalyResponse[]
}

export default function SegmentAnomaliesCard({ anomalies }: Props) {
  return (
    <div className="card">
      <div className="card-title">Segment Anomalies</div>
      {anomalies.length === 0
        ? <p className="empty-state">No segment anomalies recorded.</p>
        : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Detected ({TZ})</th>
                  <th>Gap (s)</th>
                  <th>Expected (s)</th>
                  <th>Resolved</th>
                </tr>
              </thead>
              <tbody>
                {anomalies.map(a => (
                  <tr key={a.id}>
                    <td>{fmtDate(a.detected_at)}</td>
                    <td className={a.actual_gap_seconds > 60 ? 'text-red' : ''}>
                      {a.actual_gap_seconds.toFixed(1)}
                    </td>
                    <td>{a.expected_interval_seconds.toFixed(1)}</td>
                    <td>{a.resolved ? '✓' : <span className="badge badge-error">open</span>}</td>
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
