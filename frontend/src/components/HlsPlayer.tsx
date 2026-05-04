/**
 * HlsPlayer — Phase 5.
 *
 * Plays an HLS stream served by the PGMRec backend.
 * Uses hls.js in Chromium/Firefox; falls back to native HLS for Safari.
 *
 * The JWT token is injected into every XHR request via hls.js's xhrSetup
 * callback so that auth-protected endpoints work transparently.
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
      const hls = new Hls({
        // Inject Bearer token on every XHR (playlist + segments)
        xhrSetup(xhr) {
          const token = getToken()
          if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
        },
        // Aggressive low-latency tuning for live preview
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
      // Safari native HLS — cannot inject headers, but Safari handles cookies
      video.src = playlistUrl
      video.addEventListener('loadedmetadata', () => {
        setStatus('playing')
        onReady?.()
        video.play().catch(() => { /* ignore */ })
      })
      video.addEventListener('error', () => {
        setStatus('error')
        onError?.('Video load error (native HLS)')
      })
    } else {
      setStatus('error')
      onError?.('HLS is not supported in this browser.')
    }

    return cleanup
  }, [channelId, playlistUrl])

  return (
    <>
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
