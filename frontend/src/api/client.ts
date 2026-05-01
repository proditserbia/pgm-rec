import type {
  ChannelSummary, ChannelDetailResponse, ActionResponse,
  LogsResponse, CommandPreviewResponse, ChannelHealthResponse,
  SegmentAnomalyResponse, ChannelDebugResponse, DiskUsageResponse,
  ResolveRangeRequest, ResolveRangeResponse,
  ExportJobRequest, ExportJobResponse,
  HlsPreviewStatusResponse, PreviewLogsResponse, ChannelDiagnosticsResponse,
  TokenResponse, UserResponse, ConfigReloadResponse,
} from '../types'

export const BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) || 'http://localhost:8000'

// ── Token storage ──────────────────────────────────────────────────────────
const TOKEN_KEY = 'pgmrec_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}
export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

// ── Core request helper ────────────────────────────────────────────────────
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = `Bearer ${token}`
  const res = await fetch(`${BASE}${path}`, {
    headers: { ...headers, ...options?.headers },
    ...options,
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const json = await res.json()
      if (json?.detail) detail = String(json.detail)
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

// ── Auth ───────────────────────────────────────────────────────────────────
export async function login(username: string, password: string): Promise<TokenResponse> {
  const body = new URLSearchParams({ username, password })
  const res = await fetch(`${BASE}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const json = await res.json()
      if (json?.detail) detail = String(json.detail)
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json() as Promise<TokenResponse>
}
export const getCurrentUser = () => request<UserResponse>('/api/v1/auth/me')

// ── Channels ──────────────────────────────────────────────────────────────────
export const getChannels = () => request<ChannelSummary[]>('/api/v1/channels/')
export const getChannel = (id: string) => request<ChannelDetailResponse>(`/api/v1/channels/${id}`)
export const startChannel = (id: string) => request<ActionResponse>(`/api/v1/channels/${id}/start`, { method: 'POST' })
export const stopChannel = (id: string) => request<ActionResponse>(`/api/v1/channels/${id}/stop`, { method: 'POST' })
export const restartChannel = (id: string) => request<ActionResponse>(`/api/v1/channels/${id}/restart`, { method: 'POST' })
export const getChannelLogs = (id: string, lines = 100) =>
  request<LogsResponse>(`/api/v1/channels/${id}/logs?lines=${lines}`)
export const getChannelCommand = (id: string) =>
  request<CommandPreviewResponse>(`/api/v1/channels/${id}/command`)
export const getChannelWatchdog = (id: string) =>
  request<ChannelHealthResponse>(`/api/v1/channels/${id}/watchdog`)
export const getChannelAnomalies = (id: string) =>
  request<SegmentAnomalyResponse[]>(`/api/v1/channels/${id}/anomalies`)
export const getChannelDebug = (id: string) =>
  request<ChannelDebugResponse>(`/api/v1/channels/${id}/debug`)
export const reloadChannelConfig = (id: string) =>
  request<ConfigReloadResponse>(`/api/v1/channels/${id}/reload-config`, { method: 'POST' })

// ── System ─────────────────────────────────────────────────────────────────
export const getSystemDisk = () => request<DiskUsageResponse>('/api/v1/system/disk')

// ── Exports ───────────────────────────────────────────────────────────────────
export const resolveRange = (channelId: string, body: ResolveRangeRequest) =>
  request<ResolveRangeResponse>(
    `/api/v1/channels/${channelId}/exports/resolve-range`,
    { method: 'POST', body: JSON.stringify(body) }
  )
export const createExport = (channelId: string, body: ExportJobRequest) =>
  request<ExportJobResponse>(
    `/api/v1/channels/${channelId}/exports`,
    { method: 'POST', body: JSON.stringify(body) }
  )
export const getExport = (id: number) =>
  request<ExportJobResponse>(`/api/v1/exports/${id}`)
export const listExports = (params?: { channel_id?: string; status?: string; limit?: number }) => {
  const qs = new URLSearchParams()
  if (params?.channel_id) qs.set('channel_id', params.channel_id)
  if (params?.status) qs.set('status', params.status)
  if (params?.limit != null) qs.set('limit', String(params.limit))
  const q = qs.toString() ? `?${qs}` : ''
  return request<ExportJobResponse[]>(`/api/v1/exports${q}`)
}
export const cancelExport = (id: number) =>
  request<ExportJobResponse>(`/api/v1/exports/${id}/cancel`, { method: 'POST' })
export const getExportLogsUrl = (id: number) => `${BASE}/api/v1/exports/${id}/logs`
export const getExportDownloadUrl = (id: number) => `${BASE}/api/v1/exports/${id}/download`

// ── HLS Preview — Phase 5 / Phase 9 ───────────────────────────────────────
export const startPreview = (channelId: string) =>
  request<HlsPreviewStatusResponse>(`/api/v1/channels/${channelId}/preview/start`, { method: 'POST' })
export const stopPreview = (channelId: string) =>
  request<HlsPreviewStatusResponse>(`/api/v1/channels/${channelId}/preview/stop`, { method: 'POST' })
export const getPreviewStatus = (channelId: string) =>
  request<HlsPreviewStatusResponse>(`/api/v1/channels/${channelId}/preview/status`)
export const getPreviewPlaylistUrl = (channelId: string) =>
  `${BASE}/api/v1/channels/${channelId}/preview/playlist.m3u8`
export const getPreviewLogs = (channelId: string, lines = 100) =>
  request<PreviewLogsResponse>(`/api/v1/channels/${channelId}/preview/logs?lines=${lines}`)
export const getChannelDiagnostics = (channelId: string) =>
  request<ChannelDiagnosticsResponse>(`/api/v1/channels/${channelId}/diagnostics`)
