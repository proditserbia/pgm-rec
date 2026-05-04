import type { ChannelConfig } from '../../types'

interface Props {
  config: ChannelConfig
}

export default function ChannelConfigCard({ config }: Props) {
  return (
    <div className="card">
      <div className="card-title">Channel Configuration</div>
      <div className="card-row">
        <span className="card-label">Codec</span>
        <span className="card-value">{config.encoding.video_codec} / {config.encoding.preset}</span>
      </div>
      <div className="card-row">
        <span className="card-label">Bitrate</span>
        <span className="card-value">{config.encoding.video_bitrate} video / {config.encoding.audio_bitrate} audio</span>
      </div>
      <div className="card-row">
        <span className="card-label">Input</span>
        <span className="card-value">
          {config.capture.video_device} ({config.capture.resolution} @ {config.capture.framerate}fps)
        </span>
      </div>
      <div className="card-row">
        <span className="card-label">Segment time</span>
        <span className="card-value">{config.segmentation.segment_time}</span>
      </div>
      <div className="card-row">
        <span className="card-label">Record dir</span>
        <span className="card-value" style={{ fontFamily: 'monospace', fontSize: 12 }}>{config.paths.record_dir}</span>
      </div>
      <div className="card-row">
        <span className="card-label">Timezone</span>
        <span className="card-value">{config.timezone}</span>
      </div>
      <div className="card-row">
        <span className="card-label">Preview</span>
        <span className="card-value">
          {config.preview.width}×{config.preview.height} @ {config.preview.hls_fps}fps / {config.preview.video_bitrate}
        </span>
      </div>
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
  )
}
