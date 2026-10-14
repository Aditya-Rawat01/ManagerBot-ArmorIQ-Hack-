import { config } from '../config'
import { createApiClient } from './apiClient'

/** App-wide API client configured from VITE_API_BASE_URL. */
export const apiClient = createApiClient({ baseUrl: config.apiBaseUrl })

export { ApiError, createApiClient, type ApiClient } from './apiClient'
export { fetchHealth, HEALTH_PATH } from './health'
