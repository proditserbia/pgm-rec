import type { ChannelConfig, ChannelSummary, HlsPreviewStatusResponse } from '../../types'
import HlsPlayer from '../HlsPlayer'

interface Props {
  channelId: string
  config: ChannelConfig
  summary: ChannelSummary
  previewStatus: HlsPreviewStatusResponse | null
  previewBusy: boolean
  previewError: string | null
  isAdmin: boolean
  onStartPreview: () => void
  onStopPreview: () => void
  onPlayerError: (msg: string) => void
}

function previewModeLabel(mode: string): string {
  if (mode === 'hls_direct') return 'HLS Direct'
  if (mode === 'from_udp') return 'UDP→HLS'
  if (mode === 'from_recording_output') return 'Rec→HLS'
  return mode
}

export default function LivePreviewCard({
  channelId, config, summary, previewStatus,
  previewBusy, previewError, isAdmin, onStartPreview, onStopPreview, onPlayerError,
}: Props) {
  const previewRunning = previewStatus?.running ?? false
  const previewReady = previewStatus?.playlist_ready ?? false
  const startupStatus = previewStatus?.startup_status ?? 'stopped'

  return (
    <div className="monitor-card">
      {/* Title bar */}
      <div className="monitor-titlebar">
        <div className={`monitor-live-dot dot-${startupStatus}`} />
        <span className="monitor-title">Live Preview</span>

        {startupStatus === 'running' && (
          <span className="monitor-live-label label-running">LIVE</span>
        )}
        {startupStatus === 'starting' && (
          <span className="monitor-live-label label-starting">STARTING</span>
        )}
        {startupStatus === 'failed' && (
          <span className="monitor-live-label label-failed">FAILED</span>
        )}
        {previewStatus && previewRunning && (
          <span className="monitor-status-text">· Health: {previewStatus.health}</span>
        )}

        <span className="monitor-titlebar-spacer" />

        {previewError && (
          <span style={{ fontSize: 11, color: '#ef4444', maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {previewError}
          </span>
        )}

        {isAdmin && (
          previewRunning ? (
            <button className="btn btn-danger btn-sm" disabled={previewBusy} onClick={onStopPreview}>
              ■ Stop Preview
            </button>
          ) : (
            <button className="btn btn-success btn-sm" disabled={previewBusy} onClick={onStartPreview}>
              ▶ Start Preview
            </button>
          )
        )}
      </div>

      {/* Monitor viewport */}
      <div className="monitor-viewport">
        <div className="monitor-viewport-ratio">

          {startupStatus === 'stopped' && (
            <div className="monitor-state-screen">
              <span style={{ fontSize: 28, opacity: 0.25 }}>▶</span>
              <span>{isAdmin ? 'Preview stopped · click Start Preview' : 'Preview is not running'}</span>
            </div>
          )}

          {startupStatus === 'starting' && (
            <div className="monitor-state-screen state-starting">
              <div className="monitor-spinner" />
              <span>Starting preview…</span>
              <span className="monitor-state-hint">Waiting for first HLS segment</span>
            </div>
          )}

          {startupStatus === 'failed' && (
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

          {previewReady && (
            <>
              <HlsPlayer
                channelId={channelId}
                onError={onPlayerError}
              />
              <div className="monitor-overlay-tl">
                {(summary.display_name?.toUpperCase() ?? 'CHANNEL')} LIVE
              </div>
              <div className="monitor-overlay-tr">
                {config.preview.width}×{config.preview.height} / {config.preview.hls_fps}fps
              </div>
              <div className="monitor-overlay-bl">
                {previewModeLabel(config.preview.input_mode)}
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
  )
}
