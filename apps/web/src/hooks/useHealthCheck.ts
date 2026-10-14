import { useCallback, useEffect, useState } from 'react'

import { ApiError, type ApiClient } from '../services/apiClient'
import { fetchHealth } from '../services/health'
import type { HealthResponse } from '../types'

export type HealthState =
  | { status: 'loading' }
  | { status: 'success'; data: HealthResponse }
  | { status: 'error'; error: ApiError }

function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  return new ApiError({ status: 0, code: 'unknown_error', message: 'Unexpected error.' })
}

export function useHealthCheck(client: ApiClient): { state: HealthState; retry: () => void } {
  const [state, setState] = useState<HealthState>({ status: 'loading' })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    fetchHealth(client, controller.signal)
      .then((data) => setState({ status: 'success', data }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setState({ status: 'error', error: toApiError(error) })
      })
    return () => controller.abort()
  }, [client, attempt])

  const retry = useCallback(() => {
    setState({ status: 'loading' })
    setAttempt((value) => value + 1)
  }, [])

  return { state, retry }
}
