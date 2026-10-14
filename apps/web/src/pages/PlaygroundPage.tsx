import { AppHeader } from '../components/AppHeader'
import { ConversationPlaceholder } from '../components/ConversationPlaceholder'
import { HealthStatus } from '../components/HealthStatus'
import { PlaceholderPanel } from '../components/PlaceholderPanel'
import { useHealthCheck } from '../hooks/useHealthCheck'
import type { ApiClient } from '../services/apiClient'

interface PlaygroundPageProps {
  client: ApiClient
}

export function PlaygroundPage({ client }: PlaygroundPageProps) {
  const { state, retry } = useHealthCheck(client)

  return (
    <div className="app">
      <AppHeader status={<HealthStatus state={state} onRetry={retry} />} />
      <main className="layout">
        <ConversationPlaceholder />
        <aside className="sidebar">
          <PlaceholderPanel title="Signed in as">
            <p className="muted">
              Demo sign-in (Maya, Sam, or Priya) arrives in a later phase. Identity will come from
              the server-side session, never from chat text.
            </p>
          </PlaceholderPanel>
          <PlaceholderPanel title="Request evidence">
            <p className="muted">
              Plan, authorization decision source, approval state, and tool-server execution
              evidence for each request will appear here.
            </p>
          </PlaceholderPanel>
          <PlaceholderPanel title="Manager console">
            <p className="muted">
              Pending holds (approved in the ArmorIQ console), unanswered questions, and the
              knowledge review queue.
            </p>
          </PlaceholderPanel>
        </aside>
      </main>
      <footer className="app-footer muted">
        Foundation preview · no ArmorIQ, Slack, or LLM integration is active yet.
      </footer>
    </div>
  )
}
