import type { HealthResponse } from '../types'
import { ApiError, type ApiClient } from './apiClient'

export const HEALTH_PATH = '/api/v1/health'

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  return (
    v.status === 'ok' &&
    typeof v.service === 'string' &&
    typeof v.version === 'string' &&
    typeof v.environment === 'string'
  )
}

/** Typed backend health check. Rejects responses that do not match the shared contract. */
export async function fetchHealth(client: ApiClient, signal?: AbortSignal): Promise<HealthResponse> {
  const body = await client.get<unknown>(HEALTH_PATH, { signal })
  if (!isHealthResponse(body)) {
    throw new ApiError({
      status: 200,
      code: 'invalid_response',
      message: 'The backend health response did not match the expected contract.',
    })
  }
  return body
}
