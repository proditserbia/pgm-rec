import { useState, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import {
  getChannels, resolveRange, createExport, getExport,
  getExportLogsUrl, getExportDownloadUrl, cancelExport,
} from '../api/client'
import type { ChannelSummary, ResolveRangeResponse, ExportJobResponse } from '../types'
import ErrorBanner from '../components/ErrorBanner'
import ProgressBar from '../components/ProgressBar'

const TZ = 'Europe/Belgrade'

function fmtDuration(s: number) {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60)
  if (h > 0) return `${h}h ${m}m ${sec}s`
  if (m > 0) return `${m}m ${sec}s`
  return `${sec}s`
}

function estimateSize(durationSec: number, videoBitrateKbps = 1500, audioBitrateKbps = 128) {
  const bytes = ((videoBitrateKbps + audioBitrateKbps) * 1000 / 8) * durationSec
  if (bytes >= 1e9) return `~${(bytes / 1e9).toFixed(2)} GB`
  if (bytes >= 1e6) return `~${(bytes / 1e6).toFixed(0)} MB`
  return `~${(bytes / 1024).toFixed(0)} KB`
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString('en-GB', { timeZone: TZ, hour12: false })
}

function hmsToSeconds(hms: string) {
  const [h, m, s] = hms.split(':').map(Number)
  return (h || 0) * 3600 + (m || 0) * 60 + (s || 0)
}

function statusBadgeClass(s: string) {
  if (s === 'completed') return 'badge-running'
  if (s === 'failed') return 'badge-error'
  if (s === 'cancelled') return 'badge-stopped'
  return 'badge-orange'
}

type Step = 1 | 2 | 3

export default function ExportCreate() {
  const [step, setStep] = useState<Step>(1)
  const [channels, setChannels] = useState<ChannelSummary[]>([])
  const [channelId, setChannelId] = useState('')
  const [date, setDate] = useState('')
  const [inTime, setInTime] = useState('00:00:00')
  const [outTime, setOutTime] = useState('01:00:00')
  const [allowGaps, setAllowGaps] = useState(true)
  const [resolveResult, setResolveResult] = useState<ResolveRangeResponse | null>(null)
  const [job, setJob] = useState<ExportJobResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const requestedDuration = hmsToSeconds(outTime) - hmsToSeconds(inTime)

  useEffect(() => {
    getChannels()
      .then(list => { setChannels(list); if (list.length) setChannelId(list[0].id) })
      .catch(e => setError(e instanceof Error ? e.message : 'Failed to load channels'))
  }, [])

  useEffect(() => {
    if (!job) return
    if (['completed', 'failed', 'cancelled'].includes(job.status)) {
      if (pollRef.current) clearInterval(pollRef.current)
      return
    }
    const iv = setInterval(async () => {
      try {
        const u = await getExport(job.id)
        setJob(u)
        if (['completed', 'failed', 'cancelled'].includes(u.status)) clearInterval(iv)
      } catch { /* ignore */ }
    }, 3000)
    pollRef.current = iv
    return () => clearInterval(iv)
  }, [job?.id, job?.status]) // eslint-disable-line react-hooks/exhaustive-deps

  async function handleResolve() {
    setError(null); setLoading(true)
    try {
      setResolveResult(await resolveRange(channelId, { date, in_time: inTime, out_time: outTime }))
      setStep(2)
    } catch (e) { setError(e instanceof Error ? e.message : 'Resolve failed') }
    finally { setLoading(false) }
  }

  async function handleCreateJob() {
    setError(null); setLoading(true)
    try {
      setJob(await createExport(channelId, { date, in_time: inTime, out_time: outTime, allow_gaps: allowGaps }))
      setStep(3)
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed to create export') }
    finally { setLoading(false) }
  }

  async function handleCancel() {
    if (!job) return
    try { setJob(await cancelExport(job.id)) }
    catch (e) { setError(e instanceof Error ? e.message : 'Cancel failed') }
  }

  function handleRetry() {
    setStep(1); setJob(null); setResolveResult(null); setError(null)
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>New Export</h2>
        <Link to="/exports" className="link-back">View all exports</Link>
      </div>
      {error && <ErrorBanner message={error} />}

      {/* ─── Step 1 ──────────────────────────────────────── */}
      {step === 1 && (
        <div className="card" style={{ maxWidth: 500 }}>
          <div className="card-title">Step 1 — Configure Range</div>

          <div className="form-group">
            <label>Channel</label>
            <select value={channelId} onChange={e => setChannelId(e.target.value)}>
              {channels.length === 0 && <option value="">Loading…</option>}
              {channels.map(ch => <option key={ch.id} value={ch.id}>{ch.display_name}</option>)}
            </select>
          </div>
          <div className="form-group">
            <label>Date</label>
            <input type="date" value={date} onChange={e => setDate(e.target.value)} />
          </div>
          <div className="form-group">
            <label>In time <span className="tz-label">UTC</span></label>
            <input type="time" step="1" value={inTime} onChange={e => setInTime(e.target.value)} />
          </div>
          <div className="form-group">
            <label>Out time <span className="tz-label">UTC</span></label>
            <input type="time" step="1" value={outTime} onChange={e => setOutTime(e.target.value)} />
          </div>

          {requestedDuration > 0 && (
            <div className="card-row" style={{ marginBottom: 12 }}>
              <span className="card-label">Requested duration</span>
              <span className="card-value">
                {fmtDuration(requestedDuration)}
                <span className="text-muted" style={{ marginLeft: 8 }}>{estimateSize(requestedDuration)}</span>
              </span>
            </div>
          )}

          <div className="form-group">
            <div className="form-inline">
              <input type="checkbox" id="allow-gaps" checked={allowGaps}
                onChange={e => setAllowGaps(e.target.checked)} />
              <label htmlFor="allow-gaps" style={{ margin: 0 }}>Allow gaps in recording</label>
            </div>
          </div>

          <button
            className="btn btn-primary"
            onClick={handleResolve}
            disabled={loading || !channelId || !date || requestedDuration <= 0}
          >{loading ? 'Resolving…' : 'Resolve Range'}</button>
        </div>
      )}

      {/* ─── Step 2 ──────────────────────────────────────── */}
      {step === 2 && resolveResult && (
        <div className="card" style={{ maxWidth: 700 }}>
          <div className="card-title">Step 2 — Confirm Segments</div>

          <div className="card-row"><span className="card-label">Segments</span><span className="card-value">{resolveResult.segments.length}</span></div>
          <div className="card-row">
            <span className="card-label">Duration</span>
            <span className="card-value">
              {fmtDuration(resolveResult.export_duration_seconds)}
              <span className="text-muted" style={{ marginLeft: 8 }}>{estimateSize(resolveResult.export_duration_seconds)}</span>
            </span>
          </div>
          <div className="card-row">
            <span className="card-label">Has gaps</span>
            <span className="card-value">
              {resolveResult.has_gaps
                ? <span className="badge badge-error">⚠ YES</span>
                : <span className="badge badge-healthy">NO</span>}
            </span>
          </div>

          {resolveResult.has_gaps && resolveResult.gaps.length > 0 && (
            <>
              <div className="section-title" style={{ color: '#dc3545' }}>⚠ Gaps in recording</div>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Gap start ({TZ})</th><th>Gap end ({TZ})</th><th>Duration</th></tr></thead>
                  <tbody>
                    {resolveResult.gaps.map((g, i) => (
                      <tr key={i} style={{ background: '#fff5f5' }}>
                        <td>{fmtDate(g.gap_start)}</td>
                        <td>{fmtDate(g.gap_end)}</td>
                        <td className="text-red">{fmtDuration(g.gap_seconds)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {resolveResult.segments.length > 0 && (
            <>
              <div className="section-title">Segments to include</div>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Filename</th><th>Start ({TZ})</th><th>Duration</th></tr></thead>
                  <tbody>
                    {resolveResult.segments.map((seg, i) => (
                      <tr key={i}>
                        <td style={{ fontFamily: 'monospace', fontSize: 11 }}>{seg.filename}</td>
                        <td>{fmtDate(seg.start_time)}</td>
                        <td>{fmtDuration(seg.duration_seconds)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {resolveResult.segments.length === 0 && (
            <p className="empty-state">No segments found for the requested range.</p>
          )}

          <div className="gap-8 mt-16">
            <button className="btn btn-secondary" onClick={() => setStep(1)}>← Back</button>
            <button
              className="btn btn-primary"
              onClick={handleCreateJob}
              disabled={loading || resolveResult.segments.length === 0}
            >{loading ? 'Creating…' : 'Create Export Job'}</button>
          </div>
        </div>
      )}

      {/* ─── Step 3 ──────────────────────────────────────── */}
      {step === 3 && job && (
        <div className="card" style={{ maxWidth: 480 }}>
          <div className="card-title">Step 3 — Export Job #{job.id}</div>

          <div className="card-row">
            <span className="card-label">Status</span>
            <span className="card-value"><span className={`badge ${statusBadgeClass(job.status)}`}>{job.status}</span></span>
          </div>
          <div className="card-row">
            <span className="card-label">Progress</span>
            <span className="card-value" style={{ flex: 1, minWidth: 0 }}>
              <ProgressBar percent={job.progress_percent} status={job.status} />
              <span className="text-muted">{job.progress_percent.toFixed(1)}%</span>
            </span>
          </div>
          {job.actual_duration_seconds != null && (
            <div className="card-row">
              <span className="card-label">Output duration</span>
              <span className="card-value">{fmtDuration(job.actual_duration_seconds)}</span>
            </div>
          )}
          {job.error_message && <ErrorBanner message={job.error_message} />}

          <div className="gap-8 mt-16">
            {job.status === 'completed' && (
              <button className="btn btn-success" onClick={() => window.open(getExportDownloadUrl(job.id))}>
                ⬇ Download
              </button>
            )}
            <button className="btn btn-secondary btn-sm"
              onClick={() => window.open(getExportLogsUrl(job.id), '_blank')}>
              View Logs
            </button>
            {(job.status === 'running' || job.status === 'queued') && (
              <button className="btn btn-danger btn-sm" onClick={handleCancel}>Cancel</button>
            )}
            {(job.status === 'failed' || job.status === 'cancelled') && (
              <button className="btn btn-warning btn-sm" onClick={handleRetry}>↺ Retry</button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
