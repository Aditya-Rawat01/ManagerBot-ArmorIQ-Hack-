import { PlaceholderPanel } from './PlaceholderPanel'

/** Chat shell. Nothing here talks to an agent yet. */
export function ConversationPlaceholder() {
  return (
    <PlaceholderPanel title="Conversation" className="conversation">
      <div className="conversation__empty">
        <p className="conversation__lead">Ask what the team decided, and why.</p>
        <p className="muted">
          The chat arrives in a later phase. Every answer will cite its sources, and every
          protected read will show its real ArmorIQ decision: allowed, held for Maya&apos;s
          approval, or blocked.
        </p>
      </div>
      <form className="composer" aria-label="Message composer" onSubmit={(event) => event.preventDefault()}>
        <label htmlFor="composer-input" className="visually-hidden">
          Message
        </label>
        <input
          id="composer-input"
          className="composer__input"
          placeholder="Chat is not available yet"
          disabled
        />
        <button type="submit" className="button" disabled>
          Send
        </button>
      </form>
    </PlaceholderPanel>
  )
}
