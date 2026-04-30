import type {
  ChannelSummary, ChannelDetailResponse, ActionResponse,
  LogsResponse, CommandPreviewResponse, ChannelHealthResponse,
  SegmentAnomalyResponse, ChannelDebugResponse, DiskUsageResponse,
  ResolveRangeRequest, ResolveRangeResponse,
  ExportJobRequest, ExportJobResponse,
} from '../types'

export const BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) || 'http://localhost:8000'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
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
