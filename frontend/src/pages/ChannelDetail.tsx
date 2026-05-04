import { useState, useEffect, useCallback } from 'react'
import { useParams, Link } from 'react-router-dom'
import {
  getChannel, startChannel, stopChannel, restartChannel,
  getChannelCommand, getChannelWatchdog, getChannelAnomalies,
  startPreview, stopPreview, getPreviewStatus,
  getChannelDiagnostics, reloadChannelConfig,
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

  const [command, setCommand] = useState<string | null>(null)
  const [cmdOpen, setCmdOpen] = useState(false)

  const [watchdog, setWatchdog] = useState<WatchdogEventResponse[]>([])
  const [anomalies, setAnomalies] = useState<SegmentAnomalyResponse[]>([])

  // ── Preview state ──────────────────────────────────────────────────────
  const [previewStatus, setPreviewStatus] = useState<HlsPreviewStatusResponse | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)

  // Phase 9: channel diagnostics (admin only, on-demand)
  const [diagnostics, setDiagnostics] = useState<ChannelDiagnosticsResponse | null>(null)
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false)

  // Config reload state (admin only)
  const [reloadBusy, setReloadBusy] = useState(false)
  const [reloadMsg, setReloadMsg] = useState<string | null>(null)
  const [reloadError, setReloadError] = useState<string | null>(null)

  const fetchDetail = useCallback(async () => {
    if (!id) return
    try {
      setDetail(await getChannel(id))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load channel')
    }
  }, [id])

  const fetchPreviewStatus = useCallback(async () => {
    if (!id) return
    try { setPreviewStatus(await getPreviewStatus(id)) } catch { /* ignore */ }
  }, [id])

  useEffect(() => {
    if (!id) return
    fetchDetail()
    if (isAdmin) getChannelCommand(id).then(r => setCommand(r.command_str)).catch(() => {})
    getChannelWatchdog(id).then(r => setWatchdog(r.recent_events)).catch(() => {})
    getChannelAnomalies(id).then(r => setAnomalies(r)).catch(() => {})
    fetchPreviewStatus()
    const iv = setInterval(() => {
      fetchDetail()
      fetchPreviewStatus()
    }, POLL_MS)
    return () => clearInterval(iv)
  }, [fetchDetail, fetchPreviewStatus, id, isAdmin])

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
    try {
      const s = await stopPreview(id)
      setPreviewStatus(s)
    } catch (e) {
      setPreviewError(e instanceof Error ? e.message : 'Failed to stop preview')
    } finally { setPreviewBusy(false) }
  }

  async function handleLoadDiagnostics() {
    if (!id) return
    try {
      const d = await getChannelDiagnostics(id)
      setDiagnostics(d)
      setDiagnosticsOpen(true)
    } catch { /* ignore */ }
  }

  async function handleReloadConfig() {
    if (!id) return
    setReloadBusy(true); setReloadMsg(null); setReloadError(null)
    try {
      const r = await reloadChannelConfig(id)
      setReloadMsg(r.message)
      await fetchDetail()
    } catch (e) {
      setReloadError(e instanceof Error ? e.message : 'Reload failed')
    } finally { setReloadBusy(false) }
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
          <button
            className="btn btn-secondary btn-sm"
            disabled={reloadBusy}
            title="Re-read channel JSON config from disk and apply to DB"
            onClick={handleReloadConfig}
          >
            {reloadBusy ? 'Reloading…' : '↻ Reload Config'}
          </button>
        </div>
      )}
      {reloadMsg && (
        <div style={{
          background: '#f0fff4', border: '1px solid #a8e6c0', borderRadius: 4,
          padding: '6px 12px', fontSize: 12, color: '#276749', marginBottom: 10,
        }}>
          {reloadMsg}
        </div>
      )}
      {reloadError && <ErrorBanner message={reloadError} />}

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

      {/* ── HLS Preview — broadcast monitor ──────────────────────────────── */}
      <div className="monitor-card">
        {/* Title bar */}
        <div className="monitor-titlebar">
          <div className={`monitor-live-dot dot-${previewStartupStatus}`} />
          <span className="monitor-title">Live Preview</span>
          {previewStartupStatus === 'running' && (
            <span className="monitor-live-label label-running">LIVE</span>
          )}
          {previewStartupStatus === 'starting' && (
            <span className="monitor-live-label label-starting">STARTING</span>
          )}
          {previewStartupStatus === 'failed' && (
            <span className="monitor-live-label label-failed">FAILED</span>
          )}
          {previewStatus && previewRunning && (
            <span className="monitor-status-text">
              · Health: {previewStatus.health}
            </span>
          )}
          <span className="monitor-titlebar-spacer" />
          {previewError && (
            <span style={{ fontSize: 11, color: '#ef4444', maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {previewError}
            </span>
          )}
          {isAdmin && (
            previewRunning ? (
              <button
                className="btn btn-danger btn-sm"
                disabled={previewBusy}
                onClick={handleStopPreview}
              >
                ■ Stop Preview
              </button>
            ) : (
              <button
                className="btn btn-success btn-sm"
                disabled={previewBusy}
                onClick={handleStartPreview}
              >
                ▶ Start Preview
              </button>
            )
          )}
        </div>

        {/* Monitor viewport */}
        <div className="monitor-viewport">
          <div className="monitor-viewport-ratio">

            {/* State: stopped */}
            {previewStartupStatus === 'stopped' && (
              <div className="monitor-state-screen">
                <span style={{ fontSize: 28, opacity: 0.25 }}>▶</span>
                <span>{isAdmin ? 'Preview stopped · click Start Preview' : 'Preview is not running'}</span>
              </div>
            )}

            {/* State: starting */}
            {previewStartupStatus === 'starting' && (
              <div className="monitor-state-screen state-starting">
                <div className="monitor-spinner" />
                <span>Starting preview…</span>
                <span className="monitor-state-hint">Waiting for first HLS segment</span>
              </div>
            )}

            {/* State: failed */}
            {previewStartupStatus === 'failed' && (
              <div className="monitor-state-screen state-error">
                <span style={{ fontSize: 24 }}>✕</span>
                <span>Preview failed</span>
                {previewStatus?.failed_reason && (
                  <span className="monitor-state-hint" style={{ color: '#ef4444', maxWidth: 400, textAlign: 'center' }}>
                    {previewStatus.failed_reason}
                  </span>
                )}
              </div>
            )}

            {/* Player — auto-shown once playlist is ready */}
            {previewReady && id && (
              <>
                <HlsPlayer
                  channelId={id}
                  onError={msg => setPreviewError(msg)}
                />
                {/* Overlay: top-left channel name */}
                <div className="monitor-overlay-tl">
                  {(summary?.display_name?.toUpperCase() ?? 'CHANNEL')} LIVE
                </div>
                {/* Overlay: top-right resolution/fps */}
                <div className="monitor-overlay-tr">
                  {config.preview.width}×{config.preview.height} / {config.preview.hls_fps}fps
                </div>
                {/* Overlay: bottom-left mode + health */}
                <div className="monitor-overlay-bl">
                  {config.preview.input_mode === 'hls_direct' ? 'HLS Direct'
                    : config.preview.input_mode === 'from_udp' ? 'UDP→HLS'
                    : config.preview.input_mode === 'from_recording_output' ? 'Rec→HLS'
                    : config.preview.input_mode}
                  {previewStatus && ` · ${previewStatus.health}`}
                </div>
              </>
            )}
          </div>
        </div>

        {/* Status bar */}
        {previewRunning && previewStatus && (
          <div className="monitor-statusbar">
            <span>PID {previewStatus.pid}</span>
            <span className="monitor-statusbar-sep">·</span>
            <span>{config.preview.input_mode}</span>
            <span className="monitor-statusbar-sep">·</span>
            <span style={{ color: previewStatus.health === 'healthy' ? '#22c55e' : previewStatus.health === 'down' ? '#ef4444' : '#aaa' }}>
              {previewStatus.health}
            </span>
          </div>
        )}
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
        <div className="card-row"><span className="card-label">Preview</span><span className="card-value">{config.preview.width}×{config.preview.height} @ {config.preview.hls_fps}fps / {config.preview.video_bitrate}</span></div>
        <div className="card-row">
          <span className="card-label">Preview source</span>
          <span className="card-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>
            {config.preview.input_mode}
          </span>
        </div>
        {config.preview.input_mode === 'from_udp' && (
          <div className="card-row">
            <span className="card-label">HLS mode</span>
            <span className="card-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>
              {config.preview.hls_mode ?? 'auto'}
            </span>
          </div>
        )}
        {config.recording_preview_output && (
          <>
            <div className="card-row">
              <span className="card-label">UDP preview</span>
              <span className="card-value">
                {config.recording_preview_output.enabled
                  ? <span style={{ color: '#2e7d32', fontWeight: 600 }}>enabled</span>
                  : <span style={{ color: '#888' }}>disabled</span>}
              </span>
            </div>
            {config.recording_preview_output.enabled && (
              <div className="card-row">
                <span className="card-label">UDP URL</span>
                <span className="card-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>
                  {config.recording_preview_output.url}
                </span>
              </div>
            )}
          </>
        )}
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
