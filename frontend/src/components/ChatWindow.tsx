/**
 * ChatWindow component.
 *
 * The main conversation view. Manages message state, loads conversation
 * history when viewing an archived chat, sends new messages via the streaming
 * chat API, and auto-scrolls to the latest message while the reader is still
 * at the bottom.
 *
 * Modes of operation:
 *   - New chat (historyId is null): User can send messages, which are
 *     appended to local state on success. The welcome section and its
 *     suggestion chips exist for this mode only.
 *   - Archived chat (historyId is set): The window is read-only; the input
 *     area is replaced with a notice telling the user to start a new chat.
 *     While that conversation's history is in flight the transcript shows a
 *     loading state, and a conversation that turns out to hold no messages
 *     says so, so neither state borrows the new chat's welcome section.
 *
 * The transcript itself is not an ARIA live region: announcing every token
 * makes it unusable with a screen reader. A separate polite status region
 * carries a one-line summary of the turn instead.
 */

import { useChatMessages, PHASE_LABELS } from "@/hooks/useChatMessages";
import { formatDaySeparator, isSameDay, Message } from "@/types/chat";
import MessageBubble from "./MessageBubble";
import ChatInput from "./ChatInput";
import { StopCircle, RefreshCw, ChevronDown, Shield, Lock } from "lucide-react";

interface ChatWindowProps {
  onNewChat: () => void;
  historyId?: string | null;
  onMessageSent?: () => void;
  onAuthError?: () => void;
  onHistoryLoaded?: () => void;
  onHistoryLoadError?: (message: string) => void;
}

/** Heading placed between messages that fall on different calendar days. */
function DateSeparator({ date }: { date: Date }) {
  return (
    <div className="flex items-center gap-3 py-1" data-testid="date-separator">
      <span className="h-px flex-1 bg-insurance-border" aria-hidden="true" />
      <span className="text-[11px] text-insurance-ink-tertiary">
        {formatDaySeparator(date)}
      </span>
      <span className="h-px flex-1 bg-insurance-border" aria-hidden="true" />
    </div>
  );
}

/**
 * Whether a turn was opened by the stream but has nothing in it yet: no words,
 * no tool trace, no citations, no failure.
 *
 * The typing indicator already reports that the assistant is working, so an
 * empty bubble above it is a shell with nothing to show; suppressing it keeps
 * the waiting state to one element instead of two.
 *
 * @param message - The assistant message to inspect.
 * @returns True when the message carries nothing worth a bubble.
 */
function isEmptyPlaceholderTurn(message: Message): boolean {
  return (
    message.role === "assistant" &&
    message.content.trim() === "" &&
    (message.toolCalls ?? []).length === 0 &&
    (message.sources ?? []).length === 0 &&
    !message.error
  );
}

export default function ChatWindow({
  onNewChat,
  historyId,
  onMessageSent,
  onAuthError,
  onHistoryLoaded,
  onHistoryLoadError,
}: ChatWindowProps) {
  const {
    messages,
    messagesEndRef,
    scrollRef,
    showScrollButton,
    hasError,
    isReadOnly,
    isLoading,
    isStreaming,
    streamPhase,
    statusAnnouncement,
    scrollToBottom,
    handleScroll,
    handleSendMessage,
    handleStopGeneration,
    handleRetryLast,
  } = useChatMessages({
    onNewChat,
    historyId,
    onMessageSent,
    onAuthError,
    onHistoryLoaded,
    onHistoryLoadError,
  });

  // The welcome section belongs to a new chat. An archived conversation that is
  // still loading its history must not show it, or the reader is offered
  // suggestion chips for a conversation that cannot accept a message at all.
  const isHistoryLoading = isLoading && !!historyId;

  return (
    <div
      id="chat-window"
      className="flex flex-col h-full bg-insurance-bg relative"
      data-testid="chat-window"
      aria-label="Chat window"
    >
      <div
        id="messages-container"
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto min-h-0 px-4 pl-16 pt-16 md:pl-6 md:pt-6"
        data-testid="messages-container"
      >
        {messages.length === 0 ? (
          isHistoryLoading ? (
            <div
              id="history-loading"
              className="flex flex-col items-center justify-center h-full text-center px-4 gap-3"
              data-testid="history-loading"
            >
              <div className="w-8 h-8 border-2 border-insurance-info border-t-transparent rounded-full animate-spin" />
              <span className="text-sm text-insurance-ink-secondary">
                Loading conversation…
              </span>
            </div>
          ) : isReadOnly ? (
            <div
              id="empty-archived-state"
              className="flex flex-col items-center justify-center h-full text-center px-4 gap-4"
              data-testid="empty-archived-state"
            >
              <div className="w-12 h-12 rounded-2xl bg-insurance-surface border border-insurance-border flex items-center justify-center">
                <Lock className="w-5 h-5 text-insurance-ink-secondary" />
              </div>
              <p className="text-sm text-insurance-ink-secondary max-w-sm leading-relaxed">
                This conversation has no messages yet.
              </p>
            </div>
          ) : (
            <div
              id="empty-state"
              className="flex flex-col items-center justify-center h-full text-center px-4 animate-fade-in"
              data-testid="empty-state"
            >
              <div className="mb-8">
                <div className="w-[72px] h-[72px] rounded-2xl bg-insurance-surface border border-insurance-border flex items-center justify-center mx-auto">
                  <Shield className="w-8 h-8 text-insurance-info" />
                </div>
              </div>
              <div className="space-y-2 mb-10">
                <h2 className="text-2xl font-semibold text-insurance-ink tracking-tight">
                  Welcome to OmniCare
                </h2>
                <p className="text-insurance-ink-secondary max-w-sm leading-relaxed">
                  Ask about coverage, claims, or start a new claim.
                </p>
              </div>
              <div className="flex flex-wrap gap-2 justify-center max-w-3xl">
                {[
                  "Check my recent claims",
                  "What is covered under water damage?",
                  "File a new claim",
                ].map((suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => handleSendMessage(suggestion)}
                    className="px-4 py-2.5 bg-insurance-surface border border-insurance-border rounded-xl text-sm font-medium text-insurance-ink-secondary hover:text-insurance-ink hover:border-insurance-info/40 hover:shadow-soft hover:-translate-y-0.5 transition-all duration-200"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )
        ) : (
          <div
            id="message-list"
            className="max-w-3xl mx-auto space-y-6 pb-24"
            data-testid="message-list"
          >
            {messages.map((message, index) => {
              const previous = messages[index - 1];
              const startsNewDay =
                !previous || !isSameDay(previous.timestamp, message.timestamp);
              const isLastMessage = index === messages.length - 1;
              return (
                <div key={message.id} className="space-y-6">
                  {startsNewDay && <DateSeparator date={message.timestamp} />}
                  <MessageBubble
                    message={message}
                    onRetry={hasError ? handleRetryLast : undefined}
                    bare={isEmptyPlaceholderTurn(message)}
                    tailCaret={
                      isLastMessage &&
                      isStreaming &&
                      streamPhase === "responding"
                    }
                  />
                </div>
              );
            })}
            {isStreaming && !isReadOnly && (
              <div
                id="loading-indicator"
                className="flex justify-start animate-fade-in"
                data-testid="loading-indicator"
                aria-label="Assistant is typing"
              >
                <div className="bg-insurance-surface border border-insurance-border rounded-2xl rounded-tl-sm px-4 py-3 flex items-center gap-3 shadow-subtle">
                  <div className="w-8 h-8 rounded-full bg-insurance-info/10 flex items-center justify-center flex-shrink-0">
                    <Shield className="w-4 h-4 text-insurance-info" />
                  </div>
                  <div className="flex items-center gap-1.5">
                    <div className="w-2 h-2 bg-insurance-ink-tertiary rounded-full animate-typing-dot [animation-delay:-0.3s]"></div>
                    <div className="w-2 h-2 bg-insurance-ink-tertiary rounded-full animate-typing-dot [animation-delay:-0.15s]"></div>
                    <div className="w-2 h-2 bg-insurance-ink-tertiary rounded-full animate-typing-dot"></div>
                  </div>
                  <span
                    className="text-xs text-insurance-ink-secondary"
                    data-testid="loading-phase-message"
                  >
                    {PHASE_LABELS[streamPhase]}
                  </span>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* Polite summary of the turn. The transcript deliberately stays out of
          the accessibility tree's live regions: one announcement per phase
          change is what a screen reader user can follow. */}
      <p
        id="stream-status"
        role="status"
        aria-live="polite"
        className="sr-only"
        data-testid="stream-status"
      >
        {statusAnnouncement}
      </p>

      {showScrollButton && (
        <button
          onClick={() => scrollToBottom()}
          className="absolute bottom-24 right-6 z-20 p-2 bg-insurance-surface border border-insurance-border rounded-full shadow-medium hover:shadow-lifted transition-all duration-200 hover:-translate-y-0.5"
          aria-label="Scroll to bottom"
        >
          <ChevronDown className="w-4 h-4 text-insurance-ink-secondary" />
        </button>
      )}

      <div
        id="input-container"
        className="absolute bottom-0 left-0 right-0 p-4 bg-gradient-to-t from-insurance-bg via-insurance-bg/95 to-transparent pt-10"
        data-testid="input-container"
      >
        {isReadOnly ? (
          <div className="text-center p-4 text-insurance-ink-secondary bg-insurance-surface border border-insurance-border rounded-xl max-w-3xl mx-auto flex items-center justify-center gap-2">
            <Lock className="w-4 h-4" />
            <span>
              This is an archived chat. Click &quot;New Chat&quot; to start a
              new conversation.
            </span>
          </div>
        ) : (
          <div className="max-w-3xl mx-auto">
            <ChatInput onSend={handleSendMessage} isLoading={isStreaming} />
            <div className="flex items-center justify-between mt-2.5 px-1">
              <div className="text-center text-[11px] text-insurance-ink-tertiary flex-1">
                AI-generated information. Verify important coverage and claim
                details against your policy or insurer.
              </div>
              <div className="flex items-center gap-3">
                {isStreaming && (
                  <button
                    onClick={handleStopGeneration}
                    className="flex items-center gap-1.5 text-xs text-insurance-error hover:text-insurance-ink transition-colors"
                    aria-label="Stop generating"
                  >
                    <StopCircle className="w-3.5 h-3.5" />
                    <span>Stop</span>
                  </button>
                )}
                {!isStreaming && hasError && (
                  <button
                    onClick={handleRetryLast}
                    className="flex items-center gap-1.5 text-xs text-insurance-info hover:text-insurance-ink transition-colors"
                    aria-label="Retry last message"
                  >
                    <RefreshCw className="w-3.5 h-3.5" />
                    <span>Retry</span>
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
