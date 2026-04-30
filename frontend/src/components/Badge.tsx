import type { ProcessStatus, HealthStatus } from '../types'

export function StatusBadge({ status }: { status: ProcessStatus }) {
  const cls =
    status === 'running' ? 'badge-running' :
    status === 'error' ? 'badge-error' :
    status === 'stopped' ? 'badge-stopped' : 'badge-orange'
  return <span className={`badge ${cls}`}>{status}</span>
}

export function HealthBadge({ health }: { health: HealthStatus }) {
  const cls =
    health === 'healthy'   ? 'badge-healthy' :
    health === 'unhealthy' ? 'badge-unhealthy' :
    health === 'degraded'  ? 'badge-degraded' :
    health === 'cooldown'  ? 'badge-cooldown' : 'badge-unknown'
  return <span className={`badge ${cls}`}>{health}</span>
}
