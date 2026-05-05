/**
 * ChannelCard — Dashboard broadcast-monitor card.
 *
 * Shows channel status, optional mini preview, last-segment time,
 * health info, and action buttons in a compact dark-themed card.
 */
import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import type { ChannelSummary, ChannelDebugResponse } from '../types'
import { HealthBadge } from './Badge'
import HlsPlayer from './HlsPlayer'

const TZ = 'Europe/Belgrade'

const CHANNEL_SUFFIXES = [
  ' - PRVI PROGRAM',
  ' - DRUGI PROGRAM',
  ' - TRECI PROGRAM',
  ' - TEST PROGRAM',
]

const CHANNEL_SUFFIXES_UPPER = CHANNEL_SUFFIXES.map(s => s.toUpperCase())

function stripChannelSuffix(name: string): string {
  const upper = name.toUpperCase()
  for (let i = 0; i < CHANNEL_SUFFIXES_UPPER.length; i++) {
    if (upper.endsWith(CHANNEL_SUFFIXES_UPPER[i])) {
      return name.slice(0, name.length - CHANNEL_SUFFIXES[i].length)
    }
  }
  return name
}

function fmtDate(iso: string | null) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-GB', { timeZone: TZ, hour12: false })
}

function fmtUptime(lastRestartTime: string | null): string | null {
  if (!lastRestartTime) return null
  const secs = Math.floor((Date.now() - new Date(lastRestartTime).getTime()) / 1000)
  if (secs < 0) return null
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = secs % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
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

interface Props {
  ch: ChannelSummary
  dbg: ChannelDebugResponse | undefined
  busy: boolean
  isAdmin: boolean
  onStart: () => void
  onStop: () => void
  onRestart: () => void
}

export default function ChannelCard({ ch, dbg, busy, isAdmin, onStart, onStop, onRestart }: Props) {
  const [showPreview, setShowPreview] = useState(false)

  const isRunning = ch.status === 'running'
  const isStopped = ch.status === 'stopped'
  const isAlert = ch.health === 'unhealthy' || ch.health === 'degraded'
  const isCooldown = ch.health === 'cooldown'

  const cardCls = [
    'ch-card',
    isRunning && !isAlert && !isCooldown ? 'ch-card--live' : '',
    isStopped ? 'ch-card--stopped' : '',
    isAlert ? 'ch-card--alert' : '',
    isCooldown ? 'ch-card--cooldown' : '',
  ].filter(Boolean).join(' ')

  const dotCls = isRunning && !isAlert && !isCooldown
    ? ''
    : isAlert ? 'dot-failed'
    : isCooldown ? 'dot-starting'
    : 'dot-stopped'

  const statusLabel = isRunning
    ? isAlert ? 'DEGRADED' : isCooldown ? 'COOLDOWN' : 'RECORDING'
    : isStopped ? 'STOPPED' : ch.status.toUpperCase()

  const statusLabelCls = isRunning && !isAlert && !isCooldown
    ? 'label-running'
    : isAlert ? 'label-failed'
    : isCooldown ? 'label-starting'
    : 'label-stopped'

  const displayName = stripChannelSuffix(ch.display_name)
  const uptime = isRunning ? fmtUptime(dbg?.last_restart_time ?? null) : null

  return (
    <div className={cardCls}>
      {/* ── Header ── */}
      <div className="ch-header">
        <span className={`monitor-live-dot ${dotCls}`} />
        <span className={`monitor-live-label ${statusLabelCls}`}>{statusLabel}</span>
        <span className="ch-title">{displayName}</span>
        <span className="ch-id">{ch.id}</span>
      </div>

      {/* ── Mini preview ── */}
      {showPreview && isRunning && (
        <div className="ch-preview">
          <div className="ch-preview-ratio">
            <HlsPlayer channelId={ch.id} controls={false} />
          </div>
        </div>
      )}

      {/* ── Meta / info ── */}
      <div className="ch-meta">
        {(isAlert || isCooldown) && (
          <div className="ch-alert-row">
            {isCooldown && dbg
              ? <CooldownTimer seconds={dbg.cooldown_remaining_seconds} />
              : <>
                  ⚠ <strong>{ch.health}</strong>
                  {dbg?.stall_seconds != null && dbg.stall_seconds > 0 &&
                    <> — stalled {dbg.stall_seconds.toFixed(0)}s</>}
                  {dbg?.restart_count_window != null && dbg.restart_count_window > 0 &&
                    <> — {dbg.restart_count_window} restart{dbg.restart_count_window !== 1 ? 's' : ''}</>}
                </>
            }
          </div>
        )}

        {/* Compact status row */}
        <div className="ch-status-row">
          <HealthBadge health={ch.health} />
          {uptime && (
            <span className="ch-uptime">↑ {uptime}</span>
          )}
        </div>

        <div className="ch-meta-row">
          <span className="ch-meta-label">Last segment</span>
          <span className="ch-meta-value">
            {fmtDate(dbg?.last_segment_time ?? null)}
            {dbg?.last_segment_time && <span className="tz-label">{TZ}</span>}
          </span>
        </div>

        <div className="ch-meta-row">
          <span className="ch-meta-label">Last activity</span>
          <span className="ch-meta-value">
            {fmtDate(dbg?.last_file_size_change_at ?? null)}
            {dbg?.last_file_size_change_at && <span className="tz-label">{TZ}</span>}
          </span>
        </div>

        {dbg?.stall_seconds != null && dbg.stall_seconds > 0 && (
          <div className="ch-meta-row">
            <span className="ch-meta-label">Stall</span>
            <span className={`ch-meta-value ${dbg.stall_seconds > 30 ? 'text-red' : ''}`}>
              {dbg.stall_seconds.toFixed(0)}s
            </span>
          </div>
        )}

        {ch.pid != null && (
          <div className="ch-meta-row">
            <span className="ch-meta-label">PID</span>
            <span className="ch-meta-value">{ch.pid}</span>
          </div>
        )}
      </div>

      {/* ── Actions ── */}
      <div className="ch-actions">
        {isAdmin && (
          <>
            <button className="btn btn-success btn-sm" disabled={busy || isRunning}
              onClick={onStart}>Start</button>
            <button className="btn btn-danger btn-sm" disabled={busy || isStopped}
              onClick={onStop}>Stop</button>
            <button className="btn btn-warning btn-sm" disabled={busy}
              onClick={onRestart}>Restart</button>
          </>
        )}
        {isRunning && (
          <button className="btn btn-secondary btn-sm"
            onClick={() => setShowPreview(v => !v)}>
            {showPreview ? '⬜ Preview' : '▶ Preview'}
          </button>
        )}
        <Link to={`/channels/${ch.id}`} className="btn btn-primary btn-sm"
          style={{ textDecoration: 'none', marginLeft: 'auto' }}>
          Details →
        </Link>
      </div>
    </div>
  )
}
