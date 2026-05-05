import { useState, useEffect, useCallback, useRef } from 'react'
import {
  getChannels, startChannel, stopChannel, restartChannel,
  getChannelDebug, getSystemDisk,
} from '../api/client'
import type { ChannelSummary, ChannelDebugResponse, DiskUsageResponse } from '../types'
import ErrorBanner from '../components/ErrorBanner'
import ConfirmDialog from '../components/ConfirmDialog'
import DiskWidget from '../components/DiskWidget'
import ChannelCard from '../components/ChannelCard'
import { useAuth } from '../contexts/AuthContext'

const POLL_MS = 5000

interface Confirm { channelId: string; action: 'stop' | 'restart' }

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

      <div className="channel-grid">
        {channels.map(ch => (
          <ChannelCard
            key={ch.id}
            ch={ch}
            dbg={debug[ch.id]}
            busy={!!busy[ch.id]}
            isAdmin={isAdmin}
            onStart={() => doAction(ch.id, startChannel)}
            onStop={() => requestAction(ch.id, 'stop')}
            onRestart={() => requestAction(ch.id, 'restart')}
          />
        ))}
      </div>
    </div>
  )
}
