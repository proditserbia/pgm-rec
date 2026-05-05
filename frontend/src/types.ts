// ── Channel ──────────────────────────────────────────────────────────────────

export type ProcessStatus = 'stopped' | 'starting' | 'running' | 'stopping' | 'error'
export type HealthStatus = 'healthy' | 'unhealthy' | 'degraded' | 'cooldown' | 'unknown'

export interface ChannelSummary {
  id: string
  name: string
  display_name: string
  enabled: boolean
  status: ProcessStatus
  health: HealthStatus
  pid: number | null
}

export interface ChannelStatusResponse {
  channel_id: string
  channel_name: string
  status: ProcessStatus
  health: HealthStatus
  pid: number | null
  started_at: string | null
  uptime_seconds: number | null
  last_seen_alive: string | null
  log_path: string | null
}

export interface OverlayConfig { enabled: boolean; fontsize: number; fontcolor: string }
export interface FilterConfig { deinterlace: boolean; scale_width: number; scale_height: number; overlay: OverlayConfig }
export interface CaptureConfig { device_type: string; video_device: string; audio_device: string; resolution: string; framerate: number }
export interface EncodingConfig { video_codec: string; preset: string; video_bitrate: string; audio_bitrate: string }
export interface SegmentConfig { segment_time: string; segment_atclocktime: boolean; reset_timestamps: boolean; strftime: boolean; filename_pattern: string }
export interface PathConfig { record_dir: string; chunks_dir: string; final_dir: string }
export interface RetentionConfig { enabled: boolean; days: number }
export interface PreviewConfig {
  enabled: boolean
  port: number
  scale: string
  fps: number
  width: number
  height: number
  hls_fps: number
  video_bitrate: string
  encoder: string
  segment_time: number
  list_size: number
  input_mode: 'direct_capture' | 'from_recording_output' | 'from_udp' | 'hls_direct' | 'disabled'
  fallback_to_cpu: boolean
  hls_mode: 'copy' | 'transcode' | 'auto'
}

export interface RecordingPreviewOutputConfig {
  enabled: boolean
  url: string
  format: string
  video_codec: string
  preset: string | null
  tune: string | null
  width: number
  height: number
  fps: number
  bitrate: string
  audio_enabled: boolean
  audio_codec: string
  audio_bitrate: string
  audio_sample_rate: number
  fail_safe_mode: boolean
  fallback_to_cpu: boolean
}

export interface ChannelConfig {
  id: string; name: string; display_name: string; enabled: boolean
  ffmpeg_path: string; timezone: string
  capture: CaptureConfig; encoding: EncodingConfig; filters: FilterConfig
  segmentation: SegmentConfig; paths: PathConfig; retention: RetentionConfig; preview: PreviewConfig
  recording_preview_output: RecordingPreviewOutputConfig | null
}

export interface ChannelDetailResponse {
  summary: ChannelSummary
  config: ChannelConfig
  status: ChannelStatusResponse
}

export interface ActionResponse {
  success: boolean; message: string; channel_id: string; status: ProcessStatus
}

export interface ConfigReloadResponse {
  channel_id: string
  config_changed: boolean
  message: string
  config: ChannelConfig
}

export interface LogsResponse {
  channel_id: string; log_path: string | null; lines: string[]
}

export interface CommandPreviewResponse {
  channel_id: string; command: string[]; command_str: string
}

// ── Monitoring ────────────────────────────────────────────────────────────────

export interface WatchdogEventResponse {
  id: number; channel_id: string; event_type: string; detected_at: string; details: string | null
  // Phase 7 — broadcast alert classification
  alert_type: string | null
  severity: number
}

export interface SegmentAnomalyResponse {
  id: number; channel_id: string; detected_at: string
  last_segment_time: string | null; expected_interval_seconds: number
  actual_gap_seconds: number; resolved: boolean
}

export interface ChannelHealthResponse {
  channel_id: string; channel_name: string; status: ProcessStatus; health: HealthStatus
  pid: number | null; last_seen_alive: string | null
  recent_events: WatchdogEventResponse[]
}

export interface ChannelDebugResponse {
  channel_id: string; health: HealthStatus; pid: number | null
  last_restart_time: string | null; restart_count_window: number
  cooldown_remaining_seconds: number
  last_segment_time: string | null; last_file_size: number | null
  last_file_size_change_at: string | null; stall_seconds: number | null
}

export interface DiskUsageResponse {
  path_checked: string; total_bytes: number; used_bytes: number; free_bytes: number; percent_used: number; warning?: string | null
}

// ── Exports ───────────────────────────────────────────────────────────────────

export interface ResolveRangeRequest {
  date: string; in_time: string; out_time: string
}

export interface SegmentSlice {
  filename: string; path: string; start_time: string; end_time: string; duration_seconds: number
}

export interface GapEntry {
  gap_start: string; gap_end: string; gap_seconds: number
}

export interface ResolveRangeResponse {
  channel_id: string; date: string; in_time: string; out_time: string
  segments: SegmentSlice[]; first_segment_offset_seconds: number
  export_duration_seconds: number; has_gaps: boolean; gaps: GapEntry[]
  // Phase 7 — effective range after applying pre/post roll (null when no wrap)
  effective_in_time: string | null
  effective_out_time: string | null
}

export type ExportJobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface ExportJobRequest {
  date: string; in_time: string; out_time: string; allow_gaps: boolean
  // Phase 7
  preroll_seconds?: number
  postroll_seconds?: number
  never_expires?: boolean
}

export interface ExportJobResponse {
  id: number; channel_id: string; date: string; in_time: string; out_time: string
  status: ExportJobStatus; progress_percent: number
  output_path: string | null; log_path: string | null; error_message: string | null
  has_gaps: boolean; actual_duration_seconds: number | null
  created_at: string; started_at: string | null; completed_at: string | null
  // Phase 7
  preroll_seconds: number
  postroll_seconds: number
  never_expires: boolean
}

export type PreviewHealth = 'healthy' | 'down' | 'unknown'

export interface HlsPreviewStatusResponse {
  channel_id: string
  running: boolean
  pid: number | null
  started_at: string | null
  playlist_url: string | null
  health: PreviewHealth
  // Phase 9 additions
  startup_status: 'stopped' | 'starting' | 'running' | 'failed'
  playlist_ready: boolean
  stderr_tail: string[]
  failed_reason: string | null
}

export interface PreviewLogsResponse {
  channel_id: string
  lines: string[]
}

export interface ChannelDiagnosticsResponse {
  channel_id: string
  ffmpeg_command: string
  ffmpeg_command_list: string[]
  device_type: string
  input_specifier: string
  resolution: string
  framerate: number
  record_dir: string
  latest_segment_path: string | null
  latest_segment_size_bytes: number | null
  latest_segment_mtime: string | null
  stderr_tail: string[]
  dshow_device_hint: string
  // Phase 17 — manual diagnostic command for from_udp mode
  ffplay_hint: string | null
}

// ── Auth — Phase 4 ───────────────────────────────────────────────────────────

export interface TokenResponse {
  access_token: string
  token_type: string
  username: string
  role: string
}

export interface UserResponse {
  id: number
  username: string
  role: 'admin' | 'export' | 'preview'
  is_active: boolean
}
