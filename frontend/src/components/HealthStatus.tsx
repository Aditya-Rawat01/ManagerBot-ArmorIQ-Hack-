import type { HealthState } from '../hooks/useHealthCheck'

interface HealthStatusProps {
  state: HealthState
  onRetry: () => void
}

/** Backend connectivity indicator. Reports only what the health endpoint actually returned. */
export function HealthStatus({ state, onRetry }: HealthStatusProps) {
  if (state.status === 'loading') {
    return (
      <div className="health health--loading" role="status" aria-live="polite">
        <span className="health__dot" aria-hidden="true" />
        Checking backend…
      </div>
    )
  }

  if (state.status === 'error') {
    const { error } = state
    return (
      <div className="health health--error" role="alert">
        <span className="health__dot" aria-hidden="true" />
        <span>
          Backend unreachable <span className="health__detail">({error.code})</span>
        </span>
        <button type="button" className="button button--small" onClick={onRetry}>
          Retry
        </button>
      </div>
    )
  }

  const { data } = state
  return (
    <div className="health health--ok" role="status" aria-live="polite">
      <span className="health__dot" aria-hidden="true" />
      <span>
        Backend online{' '}
        <span className="health__detail">
          {data.service} v{data.version} · {data.environment}
        </span>
      </span>
    </div>
  )
}
