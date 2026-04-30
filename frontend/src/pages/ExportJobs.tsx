import { useState, useEffect, useCallback, useRef } from 'react'
import { Link } from 'react-router-dom'
import { listExports, cancelExport, getExportLogsUrl, getExportDownloadUrl } from '../api/client'
import type { ExportJobResponse, ExportJobStatus } from '../types'
import ErrorBanner from '../components/ErrorBanner'
import ProgressBar from '../components/ProgressBar'

const TZ = 'Europe/Belgrade'
const POLL_MS = 5000

function fmtDate(iso: string | null) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-GB', { timeZone: TZ, hour12: false })
}

function fmtDuration(s: number | null) {
  if (s == null) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60)
  if (h > 0) return `${h}h ${m}m ${sec}s`
  if (m > 0) return `${m}m ${sec}s`
  return `${sec}s`
}

function statusBadgeClass(s: ExportJobStatus) {
  if (s === 'completed') return 'badge-running'
  if (s === 'failed') return 'badge-error'
  if (s === 'cancelled') return 'badge-stopped'
  if (s === 'running') return 'badge-orange badge-pulse'
  return 'badge-unknown'
}

const STATUS_OPTIONS = [
  { value: '', label: 'All statuses' },
  { value: 'queued', label: 'Queued' },
  { value: 'running', label: 'Running' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'cancelled', label: 'Cancelled' },
]

export default function ExportJobs() {
  const [jobs, setJobs] = useState<ExportJobResponse[]>([])
  const [statusFilter, setStatusFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [secondsAgo, setSecondsAgo] = useState(0)
  const busySet = useRef(new Set<number>())
  const [busyIds, setBusyIds] = useState<number[]>([])

  const fetchJobs = useCallback(async () => {
    try {
      const list = await listExports({ limit: 50, status: statusFilter || undefined })
      setJobs(list)
      setError(null)
      setUpdatedAt(new Date())
      setSecondsAgo(0)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load export jobs')
    } finally {
      setLoading(false)
    }
  }, [statusFilter])

  useEffect(() => {
    fetchJobs()
    const iv = setInterval(fetchJobs, POLL_MS)
    return () => clearInterval(iv)
  }, [fetchJobs])

  const updatedRef = useRef(updatedAt)
  updatedRef.current = updatedAt
  useEffect(() => {
    const iv = setInterval(() => {
      if (updatedRef.current)
        setSecondsAgo(Math.floor((Date.now() - updatedRef.current.getTime()) / 1000))
    }, 1000)
    return () => clearInterval(iv)
  }, [])

  async function doCancel(id: number) {
    busySet.current.add(id)
    setBusyIds(Array.from(busySet.current))
    setActionError(null)
    try { await cancelExport(id); await fetchJobs() }
    catch (e) { setActionError(e instanceof Error ? e.message : 'Cancel failed') }
    finally {
      busySet.current.delete(id)
      setBusyIds(Array.from(busySet.current))
    }
  }

  if (loading) return <div className="page">Loading…</div>

  return (
    <div className="page">
      <div className="page-header">
        <h2>Export Jobs</h2>
        {updatedAt && <span className="updated-ago">Updated {secondsAgo}s ago</span>}
        <Link to="/exports/new" className="btn btn-primary btn-sm" style={{ textDecoration: 'none', marginLeft: 'auto' }}>
          + New Export
        </Link>
      </div>

      {error && <ErrorBanner message={error} />}
      {actionError && <ErrorBanner message={actionError} />}

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 }}>
        <label style={{ fontSize: 13, color: '#555' }}>Filter:</label>
        <select
          value={statusFilter}
          onChange={e => setStatusFilter(e.target.value)}
          style={{ padding: '4px 8px', border: '1px solid #ccc', borderRadius: 4, fontSize: 13 }}
        >
          {STATUS_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </div>

      {jobs.length === 0
        ? <p className="empty-state">No export jobs found.</p>
        : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Channel</th>
                  <th>Date</th>
                  <th>Time range <span className="tz-label">UTC</span></th>
                  <th>Status</th>
                  <th style={{ minWidth: 100 }}>Progress</th>
                  <th>Duration</th>
                  <th>Created ({TZ})</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map(job => (
                  <tr key={job.id}>
                    <td>{job.id}</td>
                    <td>{job.channel_id}</td>
                    <td>{job.date}</td>
                    <td style={{ fontFamily: 'monospace', fontSize: 12 }}>{job.in_time} – {job.out_time}</td>
                    <td>
                      <span className={`badge ${statusBadgeClass(job.status)}`}>{job.status}</span>
                      {job.has_gaps && <span className="badge badge-degraded" style={{ marginLeft: 4 }}>gaps</span>}
                    </td>
                    <td>
                      <ProgressBar percent={job.progress_percent} status={job.status} />
                      <span className="text-muted">{job.progress_percent.toFixed(0)}%</span>
                    </td>
                    <td>{fmtDuration(job.actual_duration_seconds)}</td>
                    <td style={{ fontSize: 12 }}>{fmtDate(job.created_at)}</td>
                    <td>
                      <div className="gap-6">
                        {job.status === 'completed' && (
                          <button className="btn btn-success btn-sm"
                            onClick={() => window.open(getExportDownloadUrl(job.id))}
                            title="Download">⬇</button>
                        )}
                        <button className="btn btn-secondary btn-sm"
                          onClick={() => window.open(getExportLogsUrl(job.id), '_blank')}
                          title="View logs">Logs</button>
                        {(job.status === 'running' || job.status === 'queued') && (
                          <button className="btn btn-danger btn-sm"
                            disabled={busyIds.includes(job.id)}
                            onClick={() => doCancel(job.id)}
                            title="Cancel">✕</button>
                        )}
                        {(job.status === 'failed' || job.status === 'cancelled') && (
                          <Link to="/exports/new" className="btn btn-warning btn-sm"
                            style={{ textDecoration: 'none' }} title="Retry">↺</Link>
                        )}
                      </div>
                    </td>
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
