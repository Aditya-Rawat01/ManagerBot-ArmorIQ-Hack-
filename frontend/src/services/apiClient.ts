import type { ErrorResponse } from '../types'

/** Error raised for any failed backend call; mirrors the backend ErrorResponse envelope. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string | null
  readonly details: unknown

  constructor(params: {
    status: number
    code: string
    message: string
    requestId?: string | null
    details?: unknown
  }) {
    super(params.message)
    this.name = 'ApiError'
    this.status = params.status
    this.code = params.code
    this.requestId = params.requestId ?? null
    this.details = params.details
  }
}

export interface ApiClient {
  get<T>(path: string, options?: { signal?: AbortSignal }): Promise<T>
}

export interface ApiClientOptions {
  baseUrl: string
  fetchImpl?: typeof fetch
  timeoutMs?: number
}

function isErrorResponse(body: unknown): body is ErrorResponse {
  if (typeof body !== 'object' || body === null || !('error' in body)) return false
  const error = (body as { error: unknown }).error
  return (
    typeof error === 'object' &&
    error !== null &&
    typeof (error as { code?: unknown }).code === 'string' &&
    typeof (error as { message?: unknown }).message === 'string'
  )
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    return null
  }
}

export function createApiClient({
  baseUrl,
  fetchImpl = (...args) => fetch(...args),
  timeoutMs = 10_000,
}: ApiClientOptions): ApiClient {
  const root = baseUrl.replace(/\/+$/, '')

  async function request<T>(path: string, init: RequestInit, signal?: AbortSignal): Promise<T> {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(new DOMException('timeout', 'TimeoutError')), timeoutMs)
    const onCallerAbort = () => controller.abort(signal?.reason)
    signal?.addEventListener('abort', onCallerAbort, { once: true })

    let response: Response
    try {
      response = await fetchImpl(`${root}${path}`, {
        ...init,
        // Session cookie for the future demo sign-in; backend CORS lists explicit origins.
        credentials: 'include',
        headers: { Accept: 'application/json', ...init.headers },
        signal: controller.signal,
      })
    } catch (cause) {
      const timedOut = controller.signal.reason instanceof DOMException && controller.signal.reason.name === 'TimeoutError'
      if (signal?.aborted) throw cause
      throw new ApiError({
        status: 0,
        code: timedOut ? 'timeout' : 'network_error',
        message: timedOut ? 'The backend did not respond in time.' : 'Could not reach the backend.',
      })
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onCallerAbort)
    }

    const body = await readJson(response)
    const requestId = response.headers.get('X-Request-ID')

    if (!response.ok) {
      if (isErrorResponse(body)) {
        throw new ApiError({
          status: response.status,
          code: body.error.code,
          message: body.error.message,
          requestId: body.error.request_id ?? requestId,
          details: body.error.details,
        })
      }
      throw new ApiError({
        status: response.status,
        code: 'http_error',
        message: `Request failed with status ${response.status}.`,
        requestId,
      })
    }

    return body as T
  }

  return {
    get: <T>(path: string, options?: { signal?: AbortSignal }) =>
      request<T>(path, { method: 'GET' }, options?.signal),
  }
}
