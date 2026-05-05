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
import RecordingStatusCard from '../components/channel/RecordingStatusCard'
import LivePreviewCard from '../components/channel/LivePreviewCard'
import ChannelConfigCard from '../components/channel/ChannelConfigCard'
import WatchdogEventsCard from '../components/channel/WatchdogEventsCard'
import SegmentAnomaliesCard from '../components/channel/SegmentAnomaliesCard'
import DiagnosticsCard from '../components/channel/DiagnosticsCard'
import { useAuth } from '../contexts/AuthContext'

const POLL_MS = 5000

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
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const [watchdog, setWatchdog] = useState<WatchdogEventResponse[]>([])
  const [anomalies, setAnomalies] = useState<SegmentAnomalyResponse[]>([])

  const [previewStatus, setPreviewStatus] = useState<HlsPreviewStatusResponse | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)

  const [diagnostics, setDiagnostics] = useState<ChannelDiagnosticsResponse | null>(null)
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false)

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

  function handleToggleDiagnostics() {
    if (!diagnosticsOpen && !diagnostics) {
      handleLoadDiagnostics()
    } else {
      setDiagnosticsOpen(o => !o)
    }
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

      {/* Live Preview — visual focus */}
      <LivePreviewCard
        channelId={id!}
        config={config}
        summary={summary}
        previewStatus={previewStatus}
        previewBusy={previewBusy}
        previewError={previewError}
        isAdmin={isAdmin}
        onStartPreview={handleStartPreview}
        onStopPreview={handleStopPreview}
        onPlayerError={msg => setPreviewError(msg)}
      />

      {/* Recording Status */}
      <RecordingStatusCard status={status} />

      {/* Watchdog + Anomalies */}
      <WatchdogEventsCard events={watchdog} />
      <SegmentAnomaliesCard anomalies={anomalies} />

      {/* Advanced — technical sections (admin only) */}
      {isAdmin && (
        <div className="card">
          <div className="collapsible-header" onClick={() => setAdvancedOpen(o => !o)}>
            <span>{advancedOpen ? '▾' : '▸'}</span> Advanced
          </div>
          {advancedOpen && (
            <div style={{ marginTop: 12 }}>

              {/* Channel Configuration */}
              <ChannelConfigCard config={config} />

              {/* FFmpeg Command Preview */}
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

              {/* Channel Diagnostics */}
              <DiagnosticsCard
                diagnostics={diagnostics}
                open={diagnosticsOpen}
                onToggle={handleToggleDiagnostics}
                onRefresh={handleLoadDiagnostics}
              />

            </div>
          )}
        </div>
      )}
    </div>
  )
}
