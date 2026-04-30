import { useState, useEffect, useCallback, useRef } from 'react'
import { Link } from 'react-router-dom'
import {
  getChannels, startChannel, stopChannel, restartChannel,
  getChannelDebug, getSystemDisk,
} from '../api/client'
import type { ChannelSummary, ChannelDebugResponse, DiskUsageResponse } from '../types'
import { StatusBadge, HealthBadge } from '../components/Badge'
import ErrorBanner from '../components/ErrorBanner'
import ConfirmDialog from '../components/ConfirmDialog'
import DiskWidget from '../components/DiskWidget'
import { useAuth } from '../contexts/AuthContext'

const POLL_MS = 5000

interface Confirm { channelId: string; action: 'stop' | 'restart' }

function fmtDate(iso: string | null, tz = 'Europe/Belgrade') {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-GB', { timeZone: tz, hour12: false })
}

function CooldownTimer({ seconds }: { seconds: number }) {
  const [rem, setRem] = useState(Math.ceil(seconds))
  useEffect(() => {
    setRem(Math.ceil(seconds))
    const iv = setInterval(() => setRem(r => Math.max(0, r - 1)), 1000)
    return () => clearInterval(iv)
  }, [seconds])
  if (rem <= 0) return null
  return <span className="badge badge-cooldown">Cooldown {rem}s</span>
}

export default function Dashboard() {
  const { isAdmin } = useAuth()
  const [channels, setChannels] = useState<ChannelSummary[]>([])
  const [debug, setDebug] = useState<Record<string, ChannelDebugResponse>>({})
  const [disk, setDisk] = useState<DiskUsageResponse | null>(null)
  const [diskError, setDiskError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [confirm, setConfirm] = useState<Confirm | null>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [secondsAgo, setSecondsAgo] = useState(0)

  const fetchChannels = useCallback(async () => {
    try {
      const list = await getChannels()
      setChannels(list)
      setError(null)
      setUpdatedAt(new Date())
      setSecondsAgo(0)
      list.forEach(ch => {
        getChannelDebug(ch.id)
          .then(d => setDebug(prev => ({ ...prev, [ch.id]: d })))
          .catch(() => {/* ignore */})
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load channels')
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchDisk = useCallback(async () => {
    try {
      setDisk(await getSystemDisk())
      setDiskError(null)
    } catch (e) {
      setDiskError(e instanceof Error ? e.message : 'unavailable')
    }
  }, [])

  useEffect(() => {
    fetchChannels(); fetchDisk()
    const iv = setInterval(() => { fetchChannels(); fetchDisk() }, POLL_MS)
    return () => clearInterval(iv)
  }, [fetchChannels, fetchDisk])

  // "updated Xs ago" ticker
  const updatedRef = useRef(updatedAt)
  updatedRef.current = updatedAt
  useEffect(() => {
    const iv = setInterval(() => {
      if (updatedRef.current)
        setSecondsAgo(Math.floor((Date.now() - updatedRef.current.getTime()) / 1000))
    }, 1000)
    return () => clearInterval(iv)
  }, [])

  async function doAction(id: string, action: (id: string) => Promise<unknown>) {
    setBusy(b => ({ ...b, [id]: true }))
    setActionError(null)
    try { await action(id); await fetchChannels() }
    catch (e) { setActionError(e instanceof Error ? e.message : 'Action failed') }
    finally { setBusy(b => ({ ...b, [id]: false })) }
  }

  function requestAction(id: string, action: 'stop' | 'restart') {
    setConfirm({ channelId: id, action })
  }

  function onConfirm() {
    if (!confirm) return
    const { channelId, action } = confirm
    setConfirm(null)
    if (action === 'stop') doAction(channelId, stopChannel)
    else doAction(channelId, restartChannel)
  }

  if (loading) return <div className="page">Loading channels…</div>

  const TZ = 'Europe/Belgrade'

  return (
    <div className="page">
      {confirm && (
        <ConfirmDialog
          message={`Are you sure you want to ${confirm.action} recording for channel "${confirm.channelId}"?`}
          onConfirm={onConfirm}
          onCancel={() => setConfirm(null)}
        />
      )}

      <div className="page-header">
        <h2>Dashboard</h2>
        {updatedAt && <span className="updated-ago">Updated {secondsAgo}s ago</span>}
        <DiskWidget disk={disk} error={diskError} />
      </div>

      {error && <ErrorBanner message={error} />}
      {actionError && <ErrorBanner message={actionError} />}
      {channels.length === 0 && !error && <p className="empty-state">No channels configured.</p>}

      {channels.map(ch => {
        const dbg = debug[ch.id]
        const isAlert = ch.health !== 'healthy' && ch.health !== 'unknown'
        return (
          <div key={ch.id} className={`card ${isAlert ? 'alert-border' : ''}`}>
            {/* Header */}
            <div className="card-row" style={{ marginBottom: 8 }}>
              <span style={{ fontWeight: 700, fontSize: 15 }}>{ch.display_name}</span>
              <StatusBadge status={ch.status} />
              <HealthBadge health={ch.health} />
              {ch.pid != null && <span className="text-muted">PID {ch.pid}</span>}
              <span className="text-muted" style={{ marginLeft: 'auto', fontSize: 11 }}>{ch.id}</span>
            </div>

            {/* Alert row */}
            {isAlert && (
              <div className="alert alert-danger" style={{ marginBottom: 8, padding: '6px 10px', fontSize: 12 }}>
                {ch.health === 'cooldown' && dbg
                  ? <CooldownTimer seconds={dbg.cooldown_remaining_seconds} />
                  : <>
                      ⚠ Health: <strong>{ch.health}</strong>
                      {dbg?.stall_seconds != null && dbg.stall_seconds > 0 &&
                        <> — stalled {dbg.stall_seconds.toFixed(0)}s</>}
                      {dbg?.restart_count_window != null && dbg.restart_count_window > 0 &&
                        <> — {dbg.restart_count_window} restarts</>}
                    </>
                }
              </div>
            )}

            {/* Details */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px,1fr))', gap: 2, marginBottom: 8 }}>
              <div className="card-row">
                <span className="card-label">Last seen alive</span>
                <span className="card-value">
                  {fmtDate(dbg?.last_file_size_change_at ?? null)}
                  {dbg?.last_file_size_change_at && <span className="tz-label">{TZ}</span>}
                </span>
              </div>
              <div className="card-row">
                <span className="card-label">Last segment</span>
                <span className="card-value">
                  {fmtDate(dbg?.last_segment_time ?? null)}
                  {dbg?.last_segment_time && <span className="tz-label">{TZ}</span>}
                </span>
              </div>
              {dbg?.stall_seconds != null && (
                <div className="card-row">
                  <span className="card-label">Stall time</span>
                  <span className={`card-value ${dbg.stall_seconds > 30 ? 'text-red' : ''}`}>
                    {dbg.stall_seconds.toFixed(0)}s
                  </span>
                </div>
              )}
            </div>

            {/* Actions */}
            <div className="btn-group">
              {isAdmin && (
                <>
                  <button className="btn btn-success btn-sm" disabled={!!busy[ch.id] || ch.status === 'running'}
                    onClick={() => doAction(ch.id, startChannel)}>Start</button>
                  <button className="btn btn-danger btn-sm" disabled={!!busy[ch.id] || ch.status === 'stopped'}
                    onClick={() => requestAction(ch.id, 'stop')}>Stop</button>
                  <button className="btn btn-warning btn-sm" disabled={!!busy[ch.id]}
                    onClick={() => requestAction(ch.id, 'restart')}>Restart</button>
                </>
              )}
              <Link to={`/channels/${ch.id}`} className="btn btn-primary btn-sm" style={{ textDecoration: 'none' }}>
                Details →
              </Link>
            </div>
          </div>
        )
      })}
    </div>
  )
}
