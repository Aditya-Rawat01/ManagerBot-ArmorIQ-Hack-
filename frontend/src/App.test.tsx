import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import App from './App'
import { ApiError, type ApiClient } from './services/apiClient'

const healthy = { status: 'ok', service: 'proxylead-backend', version: '0.1.0', environment: 'test' }

function clientReturning(...results: Array<unknown | Error>): ApiClient {
  const get = vi.fn()
  for (const result of results) {
    if (result instanceof Error) get.mockRejectedValueOnce(result)
    else get.mockResolvedValueOnce(result)
  }
  return { get } as unknown as ApiClient
}

describe('App shell', () => {
  it('shows loading, then the backend health result', async () => {
    render(<App client={clientReturning(healthy)} />)

    expect(screen.getByText(/checking backend/i)).toBeInTheDocument()
    expect(await screen.findByText(/backend online/i)).toBeInTheDocument()
    expect(screen.getByText(/proxylead-backend v0\.1\.0 · test/)).toBeInTheDocument()
  })

  it('shows an error state with retry when the backend is unreachable', async () => {
    const user = userEvent.setup()
    const offline = new ApiError({ status: 0, code: 'network_error', message: 'offline' })
    render(<App client={clientReturning(offline, healthy)} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(/backend unreachable \(network_error\)/i)

    await user.click(screen.getByRole('button', { name: /retry/i }))
    expect(await screen.findByText(/backend online/i)).toBeInTheDocument()
  })

  it('labels unfinished areas as placeholders and keeps chat disabled', async () => {
    render(<App client={clientReturning(healthy)} />)
    await screen.findByText(/backend online/i)

    expect(screen.getAllByText('Placeholder').length).toBeGreaterThanOrEqual(3)
    expect(screen.getByRole('textbox', { name: /message/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /send/i })).toBeDisabled()
  })
})
