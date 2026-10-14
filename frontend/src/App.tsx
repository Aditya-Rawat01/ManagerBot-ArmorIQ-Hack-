import { PlaygroundPage } from './pages/PlaygroundPage'
import { apiClient as defaultClient } from './services'
import type { ApiClient } from './services/apiClient'

interface AppProps {
  /** Injectable for tests; defaults to the client configured from VITE_API_BASE_URL. */
  client?: ApiClient
}

export default function App({ client = defaultClient }: AppProps) {
  return <PlaygroundPage client={client} />
}
