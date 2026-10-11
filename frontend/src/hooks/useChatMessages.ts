/**
 * Custom hook for managing chat message state, auto-scroll, and sending logic.
 *
 * Encapsulates:
 * - Message list state
 * - Auto-scroll that follows the stream only while the reader is at the bottom
 * - Streaming chat via SSE, including tool activity and mid-stream failures
 * - Request cancellation via AbortController
 * - Keyboard shortcuts
 */

import { useState, useRef, useEffect, useCallback } from "react";
import {
  ADKSSEEvent,
  Message,
  SourceCitation,
  StoredMessage,
  StreamEvent,
  StreamPhase,
  ToolCall,
  isClaimTool,
  normalizeCitations,
} from "@/types/chat";
import { parseStreamEvent } from "@/lib/validation";
import {
  ApiError,
  ClaimCancellationNote,
  ClaimConfirmationReceipt,
  asPendingClaimConfirmation,
  getConversationHistory,
  streamMessage,
  toPendingClaimConfirmation,
} from "@/lib/api";
import { registerClaimConfirmationRecorder } from "@/components/ClaimConfirmCard";
import { useQuery } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import { useAuth } from "@/context/AuthContext";

/** Used when a stored message carries no timestamp. Module scope so it is a
 * single stable value rather than a new Date on every render. */
const FALLBACK_TIMESTAMP = new Date(0);

/**
 * Distance from the bottom, in pixels, within which the view is still
 * considered "following" the stream. Beyond it the reader has deliberately
 * moved away and every token would yank them back.
 */
const SCROLL_PIN_THRESHOLD_PX = 120;

/**
 * User-facing description of each phase of an in-flight turn.
 *
 * Exported because the loading indicator in the chat window and the polite
 * status region must describe the same phase with the same words: two copies of
 * this map is two things to keep in sync for no benefit.
 */
export const PHASE_LABELS: Record<StreamPhase, string> = {
  idle: "",
  searching: "OmniCare is searching your policy",
  preparing: "OmniCare is preparing your claim",
  responding: "OmniCare is responding",
};

/** Announced through the polite status region when a turn finishes. */
const READY_ANNOUNCEMENT = "Answer ready";

/** Announced when a turn failed after producing a partial answer. */
const FAILED_ANNOUNCEMENT =
  "The answer could not be completed. The text above is partial.";

/** Fallback note when the failure carried no user-facing message. */
const GENERIC_STREAM_ERROR =
  "Something went wrong while generating this answer. Please try again.";

/** Identifiers are unique per attempt so React keys never collide across turns. */
function createMessageId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
}

/** A fresh assistant bubble to stream into. */
function createAssistantMessage(id: string): Message {
  return { id, role: "assistant", content: "", timestamp: new Date() };
}

/** Index of the most recent user message, or ``-1`` when there is none. */
function findLastUserIndex(messages: Message[]): number {
  return [...messages].reverse().findIndex((m) => m.role === "user");
}

function appendText(prev: Message[], id: string, text: string): Message[] {
  return prev.map((m) =>
    m.id === id ? { ...m, content: m.content + text } : m,
  );
}

function appendToolCall(
  prev: Message[],
  id: string,
  call: ToolCall,
): Message[] {
  return prev.map((m) =>
    m.id === id ? { ...m, toolCalls: [...(m.toolCalls ?? []), call] } : m,
  );
}

/**
 * Attach a tool's return value to the invocation it belongs to.
 *
 * Events are keyed by tool name only, so the result is matched to the most
 * recent call of that tool still awaiting one. Without that rule a tool used
 * twice in one turn would have both results land on the first call.
 *
 * A prepared claim is stored as the structured state the confirmation card
 * renders: the claim fields live on the invocation's arguments and the token
 * and expiry on the result, and joining them here is what keeps a card from
 * having to re-read either half out of a serialised string.
 */
function attachToolResult(
  prev: Message[],
  id: string,
  tool: string,
  result: unknown,
): Message[] {
  return prev.map((m) => {
    if (m.id !== id || !m.toolCalls) return m;
    const pending = (m.toolCalls ?? []).reduce(
      (acc, call, index) =>
        call.name === tool && call.result === undefined ? index : acc,
      -1,
    );
    if (pending === -1) return m;
    const prepared = toPendingClaimConfirmation(m.toolCalls[pending], result);
    const toolCalls = m.toolCalls.map((call, index) =>
      index === pending ? { ...call, result: prepared ?? result } : call,
    );
    return { ...m, toolCalls };
  });
}

/**
 * Replace a prepared claim on its tool call with the outcome of the user's
 * decision, so the transcript records what happened rather than leaving a card
 * that can never be acted on again.
 */
function recordClaimOutcome(
  prev: Message[],
  token: string,
  outcome: ClaimConfirmationReceipt | ClaimCancellationNote,
): Message[] {
  return prev.map((m) => {
    if (!m.toolCalls) return m;
    const index = m.toolCalls.findIndex(
      (call) => asPendingClaimConfirmation(call.result)?.token === token,
    );
    if (index === -1) return m;
    const toolCalls = m.toolCalls.map((call, i) =>
      i === index ? { ...call, result: outcome } : call,
    );
    return { ...m, toolCalls };
  });
}

function attachToolError(
  prev: Message[],
  id: string,
  tool: string,
  error: string,
): Message[] {
  return prev.map((m) => {
    if (m.id !== id || !m.toolCalls) return m;
    const toolCalls = m.toolCalls.map((call) =>
      call.name === tool && call.result === undefined
        ? { ...call, result: { error } }
        : call,
    );
    // ``map`` always produces a new array, so the object is rebuilt
    // unconditionally rather than keeping an identity check that can never
    // hold.
    return { ...m, toolCalls };
  });
}

/**
 * Read a backend-reported id from a raw SSE field.
 *
 * The terminal ``response_complete`` frame carries the backend-assigned message
 * ids, but the validated stream schema (see ``parseStreamEvent``) does not
 * declare them, so Zod strips them from the parsed event: the id is only
 * readable off the raw frame.
 *
 * @param value - Raw field value from the SSE payload.
 * @returns The id when it is a non-empty string, otherwise ``undefined``.
 */
function readBackendMessageId(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/**
 * Finish a streamed answer: attach its citations and adopt its backend id.
 *
 * The id and the sources both arrive on the single terminal frame, so they are
 * applied in one pass to keep the transcript to a single commit. The backend id
 * is written to ``backendId`` rather than overwriting ``id``: ``id`` is the React
 * list key, so leaving it untouched keeps the already-rendered bubble from
 * remounting -- which would replay its enter animation -- while still handing
 * feedback the id the backend persisted and will accept.
 *
 * @param prev - The current messages.
 * @param id - The local id of the assistant bubble being finished.
 * @param backendId - The backend id of that assistant message, if persisted.
 * @param sources - Citations collected during the turn.
 * @returns The updated message list.
 */
function applyTerminalState(
  prev: Message[],
  id: string,
  backendId: string | undefined,
  sources: SourceCitation[],
): Message[] {
  return prev.map((m) =>
    m.id === id ? { ...m, ...(backendId ? { backendId } : {}), sources } : m,
  );
}

export function useChatMessages({
  onNewChat,
  historyId,
  onMessageSent,
  onAuthError,
  onHistoryLoaded,
  onHistoryLoadError,
}: {
  onNewChat: () => void;
  historyId?: string | null;
  onMessageSent?: () => void;
  onAuthError?: () => void;
  onHistoryLoaded?: () => void;
  onHistoryLoadError?: (message: string) => void;
}) {
  const { token, status } = useAuth();

  const [messages, setMessages] = useState<Message[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  /** Whether the viewport is close enough to the bottom to keep following the stream. */
  const isNearBottomRef = useRef(true);
  /**
   * Phase of the in-flight turn plus the one-line summary announced to
   * assistive technology. Kept in a single object so a phase change costs one
   * render, not two.
   */
  const [stream, setStream] = useState<{
    phase: StreamPhase;
    announcement: string;
  }>({ phase: "idle", announcement: "" });

  // Load conversation history when viewing an archived chat.
  const {
    data: historyData,
    isLoading: isLoadingHistory,
    error: historyError,
  } = useQuery({
    queryKey: ["conversation", historyId],
    queryFn: () => getConversationHistory(token!, historyId!),
    enabled: !!historyId && !!token,
  });

  // Derived rather than stored: the error state is exactly what the query reports, so
  // keeping a copy in state only risks the two disagreeing.
  const historyLoadError: string | null = historyError
    ? historyError instanceof Error
      ? historyError.message
      : "Failed to load conversation history"
    : null;

  /**
   * An archived conversation is read-only.
   *
   * The stream endpoint carries no conversation id, so a turn started here
   * would be persisted outside the conversation being viewed -- and could never
   * be found again. Declared above the handlers so their guards read as a mode
   * of the hook rather than as a value that merely happens to exist by the time
   * they run.
   */
  const isReadOnly = !!historyId;

  // Switching conversations replaces the message list. Adjusting state during render
  // is React's documented alternative to a setState-in-effect, and it avoids the extra
  // paint where the previous conversation is still on screen.
  const [renderedHistoryId, setRenderedHistoryId] = useState<
    string | null | undefined
  >(historyId);
  if (renderedHistoryId !== historyId) {
    setRenderedHistoryId(historyId);
    setMessages([]);
  }

  // Same pattern for incoming history: reconcile when the query returns new data for
  // the conversation currently being viewed.
  const [loadedHistory, setLoadedHistory] =
    useState<typeof historyData>(undefined);
  if (historyData && historyData !== loadedHistory) {
    setLoadedHistory(historyData);
    if (historyId) {
      setMessages(
        historyData.messages.map((m: StoredMessage, index: number) => ({
          // Deterministic fallbacks. Deriving these from Date.now() made the id
          // change on every re-render, which churns React keys and can duplicate
          // DOM nodes; index is stable for a given loaded conversation.
          id: m.id ?? `${historyId}-${index}`,
          // The stored id is already the backend's, so it is preferred as the id
          // feedback is sent against. It is copied onto ``backendId`` to keep the
          // invariant uniform with a freshly streamed answer (which is keyed by
          // its local id and carries the backend id separately).
          backendId: m.id ?? undefined,
          role: m.role,
          content: m.content,
          timestamp: m.timestamp ? new Date(m.timestamp) : FALLBACK_TIMESTAMP,
          error: m.error,
          sources: m.sources,
          toolCalls: m.tool_calls,
        })),
      );
    }
  }

  // Notifies the page that a finished load happened. Notifying the parent is a side
  // effect, so it stays in an effect; a ref records what was already announced so the
  // callback fires once per result instead of on every render.
  const notifiedHistoryRef = useRef<typeof historyData>(undefined);
  useEffect(() => {
    if (historyData && historyData !== notifiedHistoryRef.current) {
      notifiedHistoryRef.current = historyData;
      onHistoryLoaded?.();
    }
  }, [historyData, onHistoryLoaded]);

  useEffect(() => {
    if (historyError) {
      onHistoryLoadError?.(historyLoadError!);
    }
  }, [historyError, historyLoadError, onHistoryLoadError]);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    messagesEndRef.current?.scrollIntoView({ behavior });
  }, []);

  useEffect(() => {
    // Only follow the stream while the reader has not scrolled away. Reading the
    // ref here rather than in a scroll listener keeps the decision at the moment
    // the content grew, which is the only point it can be wrong.
    //
    // The follow itself is instant: a smooth animation re-queued on every delta
    // lags behind the text it is chasing. Smooth scrolling stays what the
    // reader's own jump-to-latest uses, so the parameter remains rather than
    // being dropped.
    if (isNearBottomRef.current) {
      scrollToBottom("auto");
    }
  }, [messages, scrollToBottom]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowScrollButton(distanceFromBottom > SCROLL_PIN_THRESHOLD_PX);
    isNearBottomRef.current = distanceFromBottom <= SCROLL_PIN_THRESHOLD_PX;
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleScroll);
  }, [handleScroll]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "n") {
        e.preventDefault();
        onNewChat();
      }
      if (
        e.key === "/" &&
        !e.ctrlKey &&
        !e.metaKey &&
        document.activeElement?.tagName !== "TEXTAREA" &&
        document.activeElement?.tagName !== "INPUT"
      ) {
        e.preventDefault();
        const textarea = document.getElementById("chat-textarea");
        if (textarea) (textarea as HTMLTextAreaElement).focus();
      }
      if (e.key === "Escape") {
        const sidebarOverlay = document.getElementById("sidebar-overlay");
        if (sidebarOverlay) sidebarOverlay.click();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onNewChat]);

  /**
   * Publish where each prepared claim in the transcript reports its outcome.
   *
   * The confirmation card is rendered by ``ToolCallBadge``, which receives only
   * the tool call, so this is the only way a decision reaches the message that
   * produced it. A token is withdrawn as soon as it leaves the transcript — a
   * new chat, a different conversation, or a resolved confirmation — so a stale
   * card can never write into an unrelated turn.
   */
  const claimRecordersRef = useRef(new Map<string, () => void>());
  useEffect(() => {
    const active = claimRecordersRef.current;
    const tokens = new Set<string>();
    for (const message of messages) {
      for (const call of message.toolCalls ?? []) {
        const pending = asPendingClaimConfirmation(call.result);
        if (pending) tokens.add(pending.token);
      }
    }

    for (const [token, withdraw] of active) {
      if (!tokens.has(token)) {
        withdraw();
        active.delete(token);
      }
    }
    for (const token of tokens) {
      if (active.has(token)) continue;
      active.set(
        token,
        registerClaimConfirmationRecorder(token, {
          recordConfirmation: (receipt) =>
            setMessages((prev) => recordClaimOutcome(prev, token, receipt)),
          recordCancellation: (note) =>
            setMessages((prev) => recordClaimOutcome(prev, token, note)),
        }),
      );
    }
  }, [messages]);

  useEffect(() => {
    const active = claimRecordersRef.current;
    return () => {
      for (const withdraw of active.values()) withdraw();
      active.clear();
    };
  }, []);

  /**
   * End any in-flight turn when the hook goes away.
   *
   * Leaving a conversation is a remount rather than a navigation: "New Chat" and
   * switching conversations key the chat window afresh, so this cleanup is the
   * one point every departure passes through. Without it the fetch keeps
   * running and its frame handlers keep writing into a component nobody can
   * see. A turn that already finished has cleared the ref, so there is nothing
   * to abort.
   */
  useEffect(() => {
    return () => {
      abortControllerRef.current?.abort();
      abortControllerRef.current = null;
    };
  }, []);

  /**
   * Start one streaming turn for an already-appended assistant bubble.
   *
   * Each turn gets a fresh idempotency key: the key exists to protect a request
   * the transport may repeat, not to make a user's deliberate retry replay the
   * answer they just rejected.
   */
  const runStream = useCallback(
    (text: string, assistantId: string) => {
      const controller = new AbortController();
      abortControllerRef.current = controller;
      // An explicit send or retry always starts pinned to the bottom; only a
      // reader who walks away mid-stream is left where they put themselves.
      isNearBottomRef.current = true;
      setStream({ phase: "searching", announcement: PHASE_LABELS.searching });
      setIsStreaming(true);

      // Whether the turn reported a failure. api.ts resolves the promise for an
      // ``error`` frame (it is followed by a terminal ``response_complete``), so
      // the success path has to ask rather than assume.
      let failed = false;

      const noteError = (message: string) => {
        failed = true;
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId && !m.error ? { ...m, error: message } : m,
          ),
        );
      };

      const onChunk = (eventData: ADKSSEEvent) => {
        const event: StreamEvent | null = parseStreamEvent(eventData);
        // Not every frame is one of ours: the backend forwards raw ADK
        // envelopes alongside the typed events.
        if (!event) return;

        switch (event.type) {
          case "text_delta":
            if (!event.content) return;
            setStream((prev) =>
              prev.phase === "responding"
                ? prev
                : {
                    phase: "responding",
                    announcement: PHASE_LABELS.responding,
                  },
            );
            setMessages((prev) => appendText(prev, assistantId, event.content));
            break;
          case "tool_call": {
            const phase: StreamPhase = isClaimTool(event.tool)
              ? "preparing"
              : "searching";
            // Once the answer has begun, later tool activity is bookkeeping
            // rather than the thing the reader is waiting on.
            setStream((prev) =>
              prev.phase === "responding"
                ? prev
                : { phase, announcement: PHASE_LABELS[phase] },
            );
            setMessages((prev) =>
              appendToolCall(prev, assistantId, {
                name: event.tool,
                arguments: event.arguments,
              }),
            );
            break;
          }
          case "tool_result":
            setMessages((prev) =>
              attachToolResult(prev, assistantId, event.tool, event.result),
            );
            break;
          case "tool_error":
            setMessages((prev) =>
              attachToolError(prev, assistantId, event.tool, event.error),
            );
            break;
          case "error":
            noteError(event.message || GENERIC_STREAM_ERROR);
            break;
          case "response_complete":
            // The terminal frame reports the backend-assigned id of the answer
            // (and the user message). Adopt the assistant id onto the bubble in
            // the same update that attaches the citations, so a just-streamed
            // answer can be rated against the row the backend actually wrote
            // rather than the locally-generated id that matches nothing.
            setMessages((prev) =>
              applyTerminalState(
                prev,
                assistantId,
                readBackendMessageId(eventData.message_id),
                normalizeCitations(event.sources),
              ),
            );
            if (event.status === "error") noteError(GENERIC_STREAM_ERROR);
            break;
        }
      };

      streamMessage(token, text, onChunk, controller.signal, createMessageId())
        .then(() => {
          setStream({
            phase: "idle",
            announcement: failed ? FAILED_ANNOUNCEMENT : READY_ANNOUNCEMENT,
          });
          if (!failed) onMessageSent?.();
        })
        .catch((error) => {
          if (error instanceof ApiError && error.code === "CANCELLED") {
            // Stopping generation keeps whatever text arrived, which is exactly
            // what the reader asked for; it is not a failure to report.
            setStream({ phase: "idle", announcement: "" });
            return;
          }
          noteError(
            error instanceof Error && error.message
              ? error.message
              : GENERIC_STREAM_ERROR,
          );
          setStream({ phase: "idle", announcement: FAILED_ANNOUNCEMENT });
          if (error instanceof ApiError && error.status === 401) {
            onAuthError?.();
          }
        })
        .finally(() => {
          abortControllerRef.current = null;
          setIsStreaming(false);
        });
    },
    [token, onMessageSent, onAuthError],
  );

  const handleSendMessage = (content: string) => {
    if (isReadOnly) return;
    if (!content.trim() || status !== "authenticated") return;

    const assistantId = createMessageId();
    const userMessage: Message = {
      id: createMessageId(),
      role: "user",
      content,
      timestamp: new Date(),
    };

    setMessages((prev) => [
      ...prev,
      userMessage,
      createAssistantMessage(assistantId),
    ]);
    runStream(content, assistantId);
  };

  const handleStopGeneration = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsStreaming(false);
    toast.success("Stopped generating");
  }, []);

  const handleRetryLast = useCallback(() => {
    // Retrying replays a turn that belongs to a live conversation; an archived
    // one has no turn to continue.
    if (isReadOnly) return;

    const lastUserMessageIndex = findLastUserIndex(messages);
    if (lastUserMessageIndex === -1) return;

    const actualIndex = messages.length - 1 - lastUserMessageIndex;
    const lastUserMessage = messages[actualIndex];
    const assistantId = createMessageId();

    // Everything after the user's turn is discarded: the partial answer and the
    // error note belong to the attempt that failed.
    setMessages((prev) => [
      ...prev.slice(0, actualIndex + 1),
      createAssistantMessage(assistantId),
    ]);
    runStream(lastUserMessage.content, assistantId);
  }, [isReadOnly, messages, runStream]);

  // Derived from the messages themselves: a note survives navigation inside the
  // transcript and a successful retry clears it without a second source of truth.
  const hasError = messages.some((m) => m.role === "error" || !!m.error);

  const isLoading = status === "loading" || isLoadingHistory;

  return {
    messages,
    setMessages,
    messagesEndRef,
    scrollRef,
    showScrollButton,
    hasError,
    isReadOnly,
    isLoading,
    isStreaming,
    streamPhase: stream.phase,
    statusAnnouncement: stream.announcement,
    historyLoadError,
    scrollToBottom,
    handleScroll,
    handleSendMessage,
    handleStopGeneration,
    handleRetryLast,
  };
}
