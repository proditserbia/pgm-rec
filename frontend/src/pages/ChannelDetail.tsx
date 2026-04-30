import { useState, useEffect, useCallback, useRef } from 'react'
import { useParams, Link } from 'react-router-dom'
import {
  getChannel, startChannel, stopChannel, restartChannel,
  getChannelLogs, getChannelCommand, getChannelWatchdog, getChannelAnomalies,
} from '../api/client'
import type { ChannelDetailResponse, WatchdogEventResponse, SegmentAnomalyResponse } from '../types'
import { StatusBadge, HealthBadge } from '../components/Badge'
import ErrorBanner from '../components/ErrorBanner'
import ConfirmDialog from '../components/ConfirmDialog'

const POLL_MS = 5000
const TZ = 'Europe/Belgrade'

function fmtDate(iso: string | null) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-GB', { timeZone: TZ, hour12: false })
}

function fmtUptime(s: number | null) {
  if (s == null) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60)
  return `${h}h ${m}m ${sec}s`
}

export default function ChannelDetail() {
  const { id } = useParams<{ id: string }>()
  const [detail, setDetail] = useState<ChannelDetailResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState<'stop' | 'restart' | null>(null)

  const [logs, setLogs] = useState<string[]>([])
  const [logsPaused, setLogsPaused] = useState(false)
  const [logLines, setLogLines] = useState(100)
  const logRef = useRef<HTMLPreElement>(null)
  const autoScroll = useRef(true)

  const [command, setCommand] = useState<string | null>(null)
  const [cmdOpen, setCmdOpen] = useState(false)

  const [watchdog, setWatchdog] = useState<WatchdogEventResponse[]>([])
  const [anomalies, setAnomalies] = useState<SegmentAnomalyResponse[]>([])

  const fetchDetail = useCallback(async () => {
    if (!id) return
    try {
      setDetail(await getChannel(id))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load channel')
    }
  }, [id])

  const fetchLogs = useCallback(async () => {
    if (!id || logsPaused) return
    try {
      const r = await getChannelLogs(id, logLines)
      setLogs(r.lines)
    } catch { /* ignore */ }
  }, [id, logLines, logsPaused])

  // Auto-scroll
  useEffect(() => {
    if (autoScroll.current && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [logs])

  useEffect(() => {
    if (!id) return
    fetchDetail(); fetchLogs()
    getChannelCommand(id).then(r => setCommand(r.command_str)).catch(() => {})
    getChannelWatchdog(id).then(r => setWatchdog(r.recent_events)).catch(() => {})
    getChannelAnomalies(id).then(r => setAnomalies(r)).catch(() => {})
    const iv = setInterval(() => { fetchDetail(); fetchLogs() }, POLL_MS)
    return () => clearInterval(iv)
  }, [fetchDetail, fetchLogs, id])

  async function doAction(action: (id: string) => Promise<unknown>) {
    if (!id) return
    setBusy(true); setActionError(null)
    try { await action(id); await fetchDetail() }
    catch (e) { setActionError(e instanceof Error ? e.message : 'Action failed') }
    finally { setBusy(false) }
  }

  function onConfirm() {
    if (!confirm) return
    const a = confirm; setConfirm(null)
    if (a === 'stop') doAction(stopChannel)
    else doAction(restartChannel)
  }

  function renderLogLine(line: string, i: number) {
    const lower = line.toLowerCase()
    const cls =
      lower.includes('error') || lower.includes('failed') ? 'log-line-error' :
      lower.includes('warn') ? 'log-line-warn' : ''
    return <div key={i} className={cls || undefined}>{line || ' '}</div>
  }

  if (!detail && !error) return <div className="page">Loading…</div>
  if (error && !detail) return <div className="page"><ErrorBanner message={error} /></div>
  if (!detail) return null

  const { summary, config, status } = detail

  return (
    <div className="page">
      {confirm && (
        <ConfirmDialog
          message={`Are you sure you want to ${confirm} recording for "${summary.display_name}"?`}
          onConfirm={onConfirm}
          onCancel={() => setConfirm(null)}
        />
      )}

      <div className="page-header">
        <Link to="/" className="link-back">← Dashboard</Link>
        <h2>{summary.display_name}</h2>
        <StatusBadge status={summary.status} />
        <HealthBadge health={summary.health} />
      </div>

      {error && <ErrorBanner message={error} />}
      {actionError && <ErrorBanner message={actionError} />}

      <div className="btn-group" style={{ marginBottom: 16 }}>
        <button className="btn btn-success btn-sm" disabled={busy || summary.status === 'running'}
          onClick={() => doAction(startChannel)}>Start</button>
        <button className="btn btn-danger btn-sm" disabled={busy || summary.status === 'stopped'}
          onClick={() => setConfirm('stop')}>Stop</button>
        <button className="btn btn-warning btn-sm" disabled={busy}
          onClick={() => setConfirm('restart')}>Restart</button>
      </div>

      {/* Status */}
      <div className="card">
        <div className="card-title">Recording Status</div>
        <div className="card-row"><span className="card-label">PID</span><span className="card-value">{status.pid ?? '—'}</span></div>
        <div className="card-row"><span className="card-label">Uptime</span><span className="card-value">{fmtUptime(status.uptime_seconds)}</span></div>
        <div className="card-row">
          <span className="card-label">Last seen alive</span>
          <span className="card-value">{fmtDate(status.last_seen_alive)}<span className="tz-label">{TZ}</span></span>
        </div>
        <div className="card-row">
          <span className="card-label">Started at</span>
          <span className="card-value">{fmtDate(status.started_at)}<span className="tz-label">{TZ}</span></span>
        </div>
      </div>

      {/* Config */}
      <div className="card">
        <div className="card-title">Channel Configuration</div>
        <div className="card-row"><span className="card-label">Codec</span><span className="card-value">{config.encoding.video_codec} / {config.encoding.preset}</span></div>
        <div className="card-row"><span className="card-label">Bitrate</span><span className="card-value">{config.encoding.video_bitrate} video / {config.encoding.audio_bitrate} audio</span></div>
        <div className="card-row"><span className="card-label">Input</span><span className="card-value">{config.capture.video_device} ({config.capture.resolution} @ {config.capture.framerate}fps)</span></div>
        <div className="card-row"><span className="card-label">Segment time</span><span className="card-value">{config.segmentation.segment_time}</span></div>
        <div className="card-row"><span className="card-label">Record dir</span><span className="card-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>{config.paths.record_dir}</span></div>
        <div className="card-row"><span className="card-label">Timezone</span><span className="card-value">{config.timezone}</span></div>
      </div>

      {/* FFmpeg command */}
      <div className="card">
        <div className="collapsible-header" onClick={() => setCmdOpen(o => !o)}>
          <span>{cmdOpen ? '▾' : '▸'}</span> FFmpeg Command Preview
        </div>
        {cmdOpen && (
          <pre className="log-block" style={{ marginTop: 10, maxHeight: 200 }}>
            {command ?? 'Loading…'}
          </pre>
        )}
      </div>

      {/* Logs */}
      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, flexWrap: 'wrap', gap: 8 }}>
          <span className="card-title" style={{ marginBottom: 0 }}>FFmpeg Log Tail</span>
          <div className="btn-group">
            <select
              value={logLines}
              onChange={e => setLogLines(Number(e.target.value))}
              style={{ fontSize: 12, padding: '2px 6px', border: '1px solid #ccc', borderRadius: 4 }}
            >
              {[50, 100, 200, 500].map(n => <option key={n} value={n}>{n} lines</option>)}
            </select>
            <button
              className={`btn btn-sm ${logsPaused ? 'btn-success' : 'btn-secondary'}`}
              onClick={() => setLogsPaused(p => !p)}
            >{logsPaused ? '▶ Resume' : '⏸ Pause'}</button>
            <button className="btn btn-sm btn-secondary" onClick={fetchLogs}>↻ Refresh</button>
          </div>
        </div>
        {logs.length === 0
          ? <p className="empty-state">No log lines available.</p>
          : (
            <pre
              ref={logRef}
              className="log-block"
              onScroll={e => {
                const el = e.currentTarget
                autoScroll.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 20
              }}
            >
              {logs.map(renderLogLine)}
            </pre>
          )
        }
      </div>

      {/* Watchdog */}
      <div className="card">
        <div className="card-title">Recent Watchdog Events</div>
        {watchdog.length === 0
          ? <p className="empty-state">No watchdog events recorded.</p>
          : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Time ({TZ})</th><th>Event</th><th>Details</th></tr></thead>
                <tbody>
                  {watchdog.map(e => (
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

      {/* Anomalies */}
      <div className="card">
        <div className="card-title">Segment Anomalies</div>
        {anomalies.length === 0
          ? <p className="empty-state">No segment anomalies recorded.</p>
          : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Detected ({TZ})</th><th>Gap (s)</th><th>Expected (s)</th><th>Resolved</th></tr></thead>
                <tbody>
                  {anomalies.map(a => (
                    <tr key={a.id}>
                      <td>{fmtDate(a.detected_at)}</td>
                      <td className={a.actual_gap_seconds > 60 ? 'text-red' : ''}>{a.actual_gap_seconds.toFixed(1)}</td>
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
    </div>
  )
}
