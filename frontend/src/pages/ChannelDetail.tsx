import { useState, useEffect, useCallback, useRef } from 'react'
import { useParams, Link } from 'react-router-dom'
import {
  getChannel, startChannel, stopChannel, restartChannel,
  getChannelLogs, getChannelCommand, getChannelWatchdog, getChannelAnomalies,
  startPreview, stopPreview, getPreviewStatus,
  getPreviewLogs, getChannelDiagnostics,
} from '../api/client'
import type {
  ChannelDetailResponse, WatchdogEventResponse, SegmentAnomalyResponse,
  HlsPreviewStatusResponse, ChannelDiagnosticsResponse,
} from '../types'
import { StatusBadge, HealthBadge } from '../components/Badge'
import ErrorBanner from '../components/ErrorBanner'
import ConfirmDialog from '../components/ConfirmDialog'
import HlsPlayer from '../components/HlsPlayer'
import { useAuth } from '../contexts/AuthContext'

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
  const { isAdmin } = useAuth()
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

  // ── Preview state ──────────────────────────────────────────────────────
  const [previewStatus, setPreviewStatus] = useState<HlsPreviewStatusResponse | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [showPlayer, setShowPlayer] = useState(false)

  // Phase 9: preview log tail (admin only, on-demand)
  const [previewLogs, setPreviewLogs] = useState<string[]>([])
  const [previewLogsOpen, setPreviewLogsOpen] = useState(false)

  // Phase 9: channel diagnostics (admin only, on-demand)
  const [diagnostics, setDiagnostics] = useState<ChannelDiagnosticsResponse | null>(null)
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false)

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
    if (!id || logsPaused || !isAdmin) return
    try {
      const r = await getChannelLogs(id, logLines)
      setLogs(r.lines)
    } catch { /* ignore */ }
  }, [id, logLines, logsPaused, isAdmin])

  const fetchPreviewStatus = useCallback(async () => {
    if (!id) return
    try { setPreviewStatus(await getPreviewStatus(id)) } catch { /* ignore */ }
  }, [id])

  // Auto-scroll
  useEffect(() => {
    if (autoScroll.current && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [logs])

  useEffect(() => {
    if (!id) return
    fetchDetail()
    if (isAdmin) fetchLogs()
    if (isAdmin) getChannelCommand(id).then(r => setCommand(r.command_str)).catch(() => {})
    getChannelWatchdog(id).then(r => setWatchdog(r.recent_events)).catch(() => {})
    getChannelAnomalies(id).then(r => setAnomalies(r)).catch(() => {})
    fetchPreviewStatus()
    const iv = setInterval(() => {
      fetchDetail()
      if (isAdmin) fetchLogs()
      fetchPreviewStatus()
    }, POLL_MS)
    return () => clearInterval(iv)
  }, [fetchDetail, fetchLogs, fetchPreviewStatus, id, isAdmin])

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

  async function handleStartPreview() {
    if (!id) return
    setPreviewBusy(true); setPreviewError(null)
    try {
      const s = await startPreview(id)
      setPreviewStatus(s)
      // Phase 9: do NOT show the player immediately — wait for playlist_ready
      // (polled via fetchPreviewStatus). This prevents the HLS fatal networkError.
    } catch (e) {
      setPreviewError(e instanceof Error ? e.message : 'Failed to start preview')
    } finally { setPreviewBusy(false) }
  }

  async function handleStopPreview() {
    if (!id) return
    setPreviewBusy(true); setPreviewError(null)
    setShowPlayer(false)
    try {
      const s = await stopPreview(id)
      setPreviewStatus(s)
    } catch (e) {
      setPreviewError(e instanceof Error ? e.message : 'Failed to stop preview')
    } finally { setPreviewBusy(false) }
  }

  async function handleLoadPreviewLogs() {
    if (!id) return
    try {
      const r = await getPreviewLogs(id, 100)
      setPreviewLogs(r.lines)
      setPreviewLogsOpen(true)
    } catch { /* ignore */ }
  }

  async function handleLoadDiagnostics() {
    if (!id) return
    try {
      const d = await getChannelDiagnostics(id)
      setDiagnostics(d)
      setDiagnosticsOpen(true)
    } catch { /* ignore */ }
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
  const previewRunning = previewStatus?.running ?? false
  const previewReady = previewStatus?.playlist_ready ?? false
  const previewStartupStatus = previewStatus?.startup_status ?? 'stopped'

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

      {isAdmin && (
        <div className="btn-group" style={{ marginBottom: 16 }}>
          <button className="btn btn-success btn-sm" disabled={busy || summary.status === 'running'}
            onClick={() => doAction(startChannel)}>Start</button>
          <button className="btn btn-danger btn-sm" disabled={busy || summary.status === 'stopped'}
            onClick={() => setConfirm('stop')}>Stop</button>
          <button className="btn btn-warning btn-sm" disabled={busy}
            onClick={() => setConfirm('restart')}>Restart</button>
        </div>
      )}

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

      {/* ── HLS Preview ──────────────────────────────────────────────────── */}
      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <span className="card-title" style={{ marginBottom: 0 }}>Live Preview</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{
              fontSize: 12, fontWeight: 600,
              color: previewStartupStatus === 'running' ? '#2e7d32'
                   : previewStartupStatus === 'failed'  ? '#c62828'
                   : previewStartupStatus === 'starting' ? '#e65100'
                   : '#666',
            }}>
              {previewStartupStatus === 'running'  ? '● Running'  :
               previewStartupStatus === 'starting' ? '◌ Starting…' :
               previewStartupStatus === 'failed'   ? '✕ Failed'   : '○ Stopped'}
            </span>
            {isAdmin && (
              <>
                {!previewRunning ? (
                  <button
                    className="btn btn-success btn-sm"
                    disabled={previewBusy}
                    onClick={handleStartPreview}
                  >
                    Start Preview
                  </button>
                ) : (
                  <button
                    className="btn btn-danger btn-sm"
                    disabled={previewBusy}
                    onClick={handleStopPreview}
                  >
                    Stop Preview
                  </button>
                )}
              </>
            )}
          </div>
        </div>

        {previewError && <ErrorBanner message={previewError} />}

        {/* Starting: waiting for playlist */}
        {previewStartupStatus === 'starting' && (
          <div style={{ textAlign: 'center', padding: '16px 0', color: '#e65100', fontSize: 13 }}>
            Starting preview… waiting for first HLS segment.
            <br />
            <span style={{ color: '#888', fontSize: 12 }}>
              (If this takes longer than 30 seconds, check preview logs below.)
            </span>
          </div>
        )}

        {/* Failed */}
        {previewStartupStatus === 'failed' && previewStatus?.failed_reason && (
          <div style={{
            background: '#fff3f3', border: '1px solid #f5c0c0', borderRadius: 4,
            padding: '8px 12px', fontSize: 12, color: '#c62828', marginBottom: 8,
          }}>
            <strong>Preview failed:</strong> {previewStatus.failed_reason}
          </div>
        )}

        {/* Ready: show "Open Player" button or actual player */}
        {previewReady && !showPlayer && (
          <div style={{ textAlign: 'center', padding: '16px 0' }}>
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => setShowPlayer(true)}
            >
              ▶ Open Player
            </button>
          </div>
        )}

        {previewReady && showPlayer && id && (
          <HlsPlayer
            channelId={id}
            onError={msg => setPreviewError(msg)}
          />
        )}

        {previewStartupStatus === 'stopped' && (
          <p className="empty-state" style={{ marginBottom: 0 }}>
            {isAdmin
              ? 'Preview is not running. Click "Start Preview" to begin.'
              : 'Preview is not running.'}
          </p>
        )}

        {previewStatus && previewRunning && (
          <div style={{ marginTop: 8, fontSize: 12, color: '#888' }}>
            PID {previewStatus.pid} &nbsp;·&nbsp; Health: {previewStatus.health}
          </div>
        )}
      </div>

      {/* ── Preview Logs (admin) ──────────────────────────────────────────── */}
      {isAdmin && (previewRunning || previewStartupStatus === 'failed') && (
      <div className="card">
        <div
          className="collapsible-header"
          onClick={() => {
            if (!previewLogsOpen) handleLoadPreviewLogs()
            setPreviewLogsOpen(o => !o)
          }}
        >
          <span>{previewLogsOpen ? '▾' : '▸'}</span> Preview FFmpeg Log Tail
        </div>
        {previewLogsOpen && (
          <>
            <button
              className="btn btn-sm btn-secondary"
              style={{ margin: '6px 0' }}
              onClick={handleLoadPreviewLogs}
            >
              ↻ Refresh
            </button>
            {previewLogs.length === 0
              ? <p className="empty-state">No preview log lines available.</p>
              : <pre className="log-block" style={{ marginTop: 6, maxHeight: 300 }}>
                  {previewLogs.map((l, i) => <div key={i}>{l || ' '}</div>)}
                </pre>
            }
          </>
        )}
      </div>
      )}

      {/* Config */}
      <div className="card">
        <div className="card-title">Channel Configuration</div>
        <div className="card-row"><span className="card-label">Codec</span><span className="card-value">{config.encoding.video_codec} / {config.encoding.preset}</span></div>
        <div className="card-row"><span className="card-label">Bitrate</span><span className="card-value">{config.encoding.video_bitrate} video / {config.encoding.audio_bitrate} audio</span></div>
        <div className="card-row"><span className="card-label">Input</span><span className="card-value">{config.capture.video_device} ({config.capture.resolution} @ {config.capture.framerate}fps)</span></div>
        <div className="card-row"><span className="card-label">Segment time</span><span className="card-value">{config.segmentation.segment_time}</span></div>
        <div className="card-row"><span className="card-label">Record dir</span><span className="card-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>{config.paths.record_dir}</span></div>
        <div className="card-row"><span className="card-label">Timezone</span><span className="card-value">{config.timezone}</span></div>
        <div className="card-row"><span className="card-label">Preview</span><span className="card-value">{config.preview.width}×{config.preview.height} @ {config.preview.hls_fps}fps / {config.preview.video_bitrate}</span></div>
      </div>

      {/* FFmpeg command — admin only */}
      {isAdmin && (
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
      )}

      {/* Logs — admin only */}
      {isAdmin && (
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
      )}

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

      {/* ── Channel Diagnostics (admin) ───────────────────────────────────── */}
      {isAdmin && (
      <div className="card">
        <div
          className="collapsible-header"
          onClick={() => {
            if (!diagnosticsOpen) handleLoadDiagnostics()
            setDiagnosticsOpen(o => !o)
          }}
        >
          <span>{diagnosticsOpen ? '▾' : '▸'}</span> Channel Diagnostics
        </div>
        {diagnosticsOpen && diagnostics && (
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
              <span className="card-value">{diagnostics.latest_segment_mtime ? fmtDate(diagnostics.latest_segment_mtime) : '—'}</span>
            </div>
            <div style={{ marginTop: 8, fontSize: 12, color: '#666' }}>
              <strong>Device listing hint (run on recording machine):</strong>
              <pre className="log-block" style={{ marginTop: 4, maxHeight: 60 }}>{diagnostics.dshow_device_hint}</pre>
            </div>
            <div style={{ marginTop: 8, fontSize: 12, fontWeight: 600, color: '#444' }}>FFmpeg command:</div>
            <pre className="log-block" style={{ maxHeight: 120, marginTop: 4 }}>{diagnostics.ffmpeg_command}</pre>
            <div style={{ marginTop: 8, fontSize: 12, fontWeight: 600, color: '#444' }}>Last recording stderr (100 lines):</div>
            {diagnostics.stderr_tail.length === 0
              ? <p className="empty-state">No recording log available.</p>
              : <pre className="log-block" style={{ maxHeight: 300, marginTop: 4 }}>
                  {diagnostics.stderr_tail.map((l, i) => <div key={i}>{l || ' '}</div>)}
                </pre>
            }
            <div style={{ marginTop: 8 }}>
              <button className="btn btn-sm btn-secondary" onClick={handleLoadDiagnostics}>↻ Refresh</button>
            </div>
          </div>
        )}
      </div>
      )}
    </div>
  )
}
