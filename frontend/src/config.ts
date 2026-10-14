/** Build-time configuration. Only VITE_* variables reach the browser bundle — never secrets. */

const DEFAULT_API_BASE_URL = 'http://localhost:8000'

export function resolveApiBaseUrl(raw: string | undefined): string {
  const value = raw?.trim() || DEFAULT_API_BASE_URL
  return value.replace(/\/+$/, '')
}

export const config = {
  apiBaseUrl: resolveApiBaseUrl(import.meta.env.VITE_API_BASE_URL),
} as const
