import type { ChannelDiagnosticsResponse } from '../../types'
import { fmtDate } from '../../utils/format'

interface Props {
  diagnostics: ChannelDiagnosticsResponse | null
  open: boolean
  onToggle: () => void
  onRefresh: () => void
}

export default function DiagnosticsCard({ diagnostics, open, onToggle, onRefresh }: Props) {
  return (
    <div className="card">
      <div
        className="collapsible-header"
        onClick={onToggle}
      >
        <span>{open ? '▾' : '▸'}</span> Channel Diagnostics
      </div>
      {open && diagnostics && (
        <div style={{ marginTop: 10 }}>
          <div className="card-row">
            <span className="card-label">Device type</span>
            <span className="card-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>{diagnostics.device_type}</span>
          </div>
          <div className="card-row">
            <span className="card-label">Input specifier</span>
            <span className="card-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>{diagnostics.input_specifier}</span>
          </div>
          <div className="card-row">
            <span className="card-label">Resolution / fps</span>
            <span className="card-value">{diagnostics.resolution} @ {diagnostics.framerate} fps</span>
          </div>
          <div className="card-row">
            <span className="card-label">Record dir</span>
            <span className="card-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>{diagnostics.record_dir}</span>
          </div>
          <div className="card-row">
            <span className="card-label">Latest segment</span>
            <span className="card-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>
              {diagnostics.latest_segment_path
                ? `${diagnostics.latest_segment_path} (${diagnostics.latest_segment_size_bytes?.toLocaleString()} bytes)`
                : '— none found'}
            </span>
          </div>
          <div className="card-row">
            <span className="card-label">Latest segment mtime</span>
            <span className="card-value">
              {diagnostics.latest_segment_mtime ? fmtDate(diagnostics.latest_segment_mtime) : '—'}
            </span>
          </div>
          <div style={{ marginTop: 8, fontSize: 12, color: '#666' }}>
            {diagnostics.ffplay_hint ? (
              <>
                <strong>UDP stream diagnostic (run on recording machine):</strong>
                <pre className="log-block" style={{ marginTop: 4, maxHeight: 60 }}>{diagnostics.ffplay_hint}</pre>
              </>
            ) : (
              <>
                <strong>Device listing hint (run on recording machine):</strong>
                <pre className="log-block" style={{ marginTop: 4, maxHeight: 60 }}>{diagnostics.dshow_device_hint}</pre>
              </>
            )}
          </div>
          <div style={{ marginTop: 8, fontSize: 12, fontWeight: 600, color: '#444' }}>FFmpeg command:</div>
          <pre className="log-block" style={{ maxHeight: 120, marginTop: 4 }}>{diagnostics.ffmpeg_command}</pre>
          <div style={{ marginTop: 8, fontSize: 12, fontWeight: 600, color: '#444' }}>Last recording stderr (100 lines):</div>
          {diagnostics.stderr_tail.length === 0
            ? <p className="empty-state">No recording log available.</p>
            : (
              <pre className="log-block" style={{ maxHeight: 300, marginTop: 4 }}>
                {diagnostics.stderr_tail.map((l, i) => <div key={i}>{l || ' '}</div>)}
              </pre>
            )
          }
          <div style={{ marginTop: 8 }}>
            <button className="btn btn-sm btn-secondary" onClick={onRefresh}>↻ Refresh</button>
          </div>
        </div>
      )}
    </div>
  )
}
