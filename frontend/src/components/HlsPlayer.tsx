/**
 * HlsPlayer — Phase 5.
 *
 * Plays an HLS stream served by the PGMRec backend.
 * Prefers hls.js (Chrome/Edge/Firefox) so that the Bearer token can be
 * injected into every XHR request.  Falls back to native HLS only on Safari
 * (where hls.js / MSE is unavailable), with a warning that auth-protected
 * streams may not work without cookies.
 */
import { useEffect, useRef, useState } from 'react'
import Hls from 'hls.js'
import { getToken, BASE } from '../api/client'

interface Props {
  channelId: string
  /** Called when the stream becomes playable */
  onReady?: () => void
  /** Called on fatal HLS errors */
  onError?: (msg: string) => void
}

export default function HlsPlayer({ channelId, onReady, onError }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)
  const [status, setStatus] = useState<'loading' | 'playing' | 'error'>('loading')
  const [safariWarning, setSafariWarning] = useState(false)

  const playlistUrl = `${BASE}/api/v1/channels/${channelId}/preview/playlist.m3u8`

  useEffect(() => {
    const video = videoRef.current
    if (!video) return

    function cleanup() {
      if (hlsRef.current) {
        hlsRef.current.destroy()
        hlsRef.current = null
      }
    }

    if (Hls.isSupported()) {
      // Chrome / Edge / Firefox — inject Bearer token on every XHR
      const hls = new Hls({
        xhrSetup(xhr) {
          const token = getToken()
          if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
        },
        // Low-latency tuning for live preview
        liveSyncDurationCount: 2,
        liveMaxLatencyDurationCount: 5,
        maxBufferLength: 10,
        maxMaxBufferLength: 20,
      })
      hlsRef.current = hls

      hls.loadSource(playlistUrl)
      hls.attachMedia(video)

      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        setStatus('playing')
        onReady?.()
        video.play().catch(() => { /* autoplay may be blocked */ })
      })

      hls.on(Hls.Events.ERROR, (_evt, data) => {
        if (data.fatal) {
          const msg = `HLS fatal error: ${data.type} / ${data.details}`
          setStatus('error')
          onError?.(msg)
          cleanup()
        }
      })
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari native HLS — cannot inject Bearer headers; warn the user
      setSafariWarning(true)
      video.src = playlistUrl
      video.addEventListener('loadedmetadata', () => {
        setStatus('playing')
        onReady?.()
        video.play().catch(() => { /* ignore */ })
      })
      video.addEventListener('error', () => {
        setStatus('error')
        onError?.('Video load error — Safari native HLS cannot send auth headers. Use Chrome, Edge, or Firefox for authenticated HLS.')
      })
    } else {
      setStatus('error')
      onError?.('HLS is not supported in this browser.')
    }

    return cleanup
  }, [channelId, playlistUrl])

  return (
    <>
      {safariWarning && status !== 'error' && (
        <div style={{
          position: 'absolute', top: 0, left: 0, right: 0, zIndex: 20,
          background: 'rgba(180,90,0,0.85)', color: '#fff',
          fontSize: 11, padding: '4px 10px', textAlign: 'center',
        }}>
          Safari: authenticated HLS may not work — use Chrome, Edge, or Firefox for full support.
        </div>
      )}
      {status === 'loading' && (
        <div className="monitor-state-screen state-starting">
          <div className="monitor-spinner" />
          <span>Buffering…</span>
        </div>
      )}
      <video
        ref={videoRef}
        muted
        playsInline
        controls
      />
    </>
  )
}
