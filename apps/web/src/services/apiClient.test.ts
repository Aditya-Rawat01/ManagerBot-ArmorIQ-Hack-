import { describe, expect, it, vi } from 'vitest'

import { resolveApiBaseUrl } from '../config'
import { ApiError, createApiClient } from './apiClient'
import { fetchHealth } from './health'

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-12345678' },
    ...init,
  })
}

const health = { status: 'ok', service: 'proxylead-backend', version: '0.1.0', environment: 'test' }

describe('resolveApiBaseUrl', () => {
  it('defaults to the local backend and strips trailing slashes', () => {
    expect(resolveApiBaseUrl(undefined)).toBe('http://localhost:8000')
    expect(resolveApiBaseUrl('  ')).toBe('http://localhost:8000')
    expect(resolveApiBaseUrl('https://api.example.com//')).toBe('https://api.example.com')
  })
})

describe('createApiClient', () => {
  it('calls the configured base URL and returns parsed JSON', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(health))
    const client = createApiClient({ baseUrl: 'http://api.test/', fetchImpl })

    await expect(fetchHealth(client)).resolves.toEqual(health)
    expect(fetchImpl).toHaveBeenCalledWith(
      'http://api.test/api/v1/health',
      expect.objectContaining({ method: 'GET', credentials: 'include' }),
    )
  })

  it('maps the backend error envelope to ApiError', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(
        { error: { code: 'not_found', message: 'Not Found', request_id: 'req-abcdef12', details: null } },
        { status: 404 },
      ),
    )
    const client = createApiClient({ baseUrl: 'http://api.test', fetchImpl })

    const error = await client.get('/missing').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 404, code: 'not_found', requestId: 'req-abcdef12' })
  })

  it('falls back to a generic error for non-envelope failures', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response('boom', { status: 502 }))
    const client = createApiClient({ baseUrl: 'http://api.test', fetchImpl })

    await expect(client.get('/x')).rejects.toMatchObject({ status: 502, code: 'http_error' })
  })

  it('reports network failures as network_error', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('Failed to fetch'))
    const client = createApiClient({ baseUrl: 'http://api.test', fetchImpl })

    await expect(client.get('/x')).rejects.toMatchObject({ status: 0, code: 'network_error' })
  })

  it('rejects a health payload that does not match the contract', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ status: 'maybe' }))
    const client = createApiClient({ baseUrl: 'http://api.test', fetchImpl })

    await expect(fetchHealth(client)).rejects.toMatchObject({ code: 'invalid_response' })
  })
})
