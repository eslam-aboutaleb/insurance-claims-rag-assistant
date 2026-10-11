/**
 * TypeScript interfaces for the OmniCare chat domain.
 *
 * These types define the shape of data exchanged between the frontend and
 * backend, as well as the internal message representation used by React
 * components. They should be kept in sync with the Pydantic schemas in
 * ``backend/app/schemas/models.py``.
 *
 * The formatting and normalisation helpers at the end of this file live here
 * rather than in a separate module: messages, the sidebar and the source pills
 * all need to render the same instant the same way, and a single definition is
 * the only thing that stops them drifting apart into three mutually
 * inconsistent notions of "now".
 */

import {
  GET_CLAIM_STATUS_TOOL,
  PREPARE_CLAIM_SUBMISSION_TOOL,
  SEARCH_CLAIMS_TOOL,
} from "@/lib/api";

/**
 * Represents a single tool invocation by the AI agent.
 *
 * The agent can call multiple tools in sequence; each call is recorded with
 * its name, input arguments, and optional result for transparency.
 */
export interface ToolCall {
  /** The registered name of the agent tool (e.g., "query_policy"). */
  name: string;
  /** The arguments passed to the tool by the agent. */
  arguments: Record<string, unknown>;
  /** The tool's return value, present after execution completes. */
  result?: unknown;
}

/**
 * A single source citation attached to an assistant answer.
 *
 * The agent normally returns a bare citation string. When the retrieval layer
 * also hands back the chunk it read, the citation arrives as an object that
 * carries an excerpt the UI can reveal on demand.
 */
export type SourceCitation =
  string | { title: string; excerpt?: string; section?: string };

/** A citation after normalisation, ready for the sources pill UI. */
export interface CitationSummary {
  /** The text shown on the pill itself. */
  label: string;
  /** Full description, used as the pill's ``title`` attribute. */
  title: string;
  /** Retrieved excerpt, present only when the backend supplied one. */
  excerpt?: string;
}

/** The result payload of the ``get_claim_status`` tool. */
export interface ClaimStatusResult {
  /** Whether the lookup found a claim. */
  found?: boolean;
  /** Human-readable claim identifier, e.g. ``"CLM-8821"``. */
  claim_id?: string;
  /** The policyholder's policy number. */
  policy_number?: string;
  /** Category of the claim, e.g. ``"Water Damage"``. */
  claim_type?: string;
  /** Processing status, e.g. ``"Submitted"``. */
  status?: string;
  /** Claimed amount, serialised as either a number or a decimal string. */
  amount?: number | string;
  /** Citation string suitable for grounding the answer. */
  citation?: string;
  /** Failure description; set instead of the claim fields on a failed lookup. */
  error?: string;
}

/**
 * A single message in a chat conversation.
 *
 * Messages are rendered by ``MessageBubble`` and stored in the conversation's
 * JSONB array on the backend.
 */
export interface Message {
  /** Unique identifier for this message. */
  id: string;
  /**
   * The backend-assigned id for this message, adopted from the server once it
   * reports it (a streaming turn's ``response_complete`` frame, or the
   * conversation detail response for history).
   *
   * The feedback endpoint authorises on the id the database persisted, so a
   * rating must be sent against this value when it is present rather than the
   * locally-generated ``id``. The two are different by construction: ``id`` is
   * minted on the client so streaming can append before the server has chosen
   * its own, while ``backendId`` is the row the server can find. Keeping them
   * separate (rather than overwriting ``id``) leaves ``id`` stable as the React
   * list key, so adopting the real id never remounts -- and so never replays the
   * enter animation on -- an already-rendered bubble.
   */
  backendId?: string;
  /** Who sent the message: the user, the assistant, or an error marker. */
  role: "user" | "assistant" | "error";
  /** The rendered text content of the message. */
  content: string;
  /**
   * Failure note rendered beneath whatever answer was produced. Never
   * replaces ``content``, so a partial answer survives a mid-stream failure.
   */
  error?: string;
  /** Optional list of policy source citations attached to assistant messages. */
  sources?: SourceCitation[];
  /** Optional list of tool calls made while generating this response. */
  toolCalls?: ToolCall[];
  /** ISO timestamp of when the message was created. */
  timestamp: Date;
}

/**
 * Wire shape of a stored conversation message.
 *
 * Timestamps travel as ISO strings and the tool call list uses the backend's
 * snake_case field name, so the stored form is deliberately distinct from
 * ``Message``.
 */
export interface StoredMessage {
  id: string;
  role: Message["role"];
  content: string;
  timestamp: string;
  error?: string;
  sources?: SourceCitation[];
  tool_calls?: ToolCall[];
}

/**
 * Lightweight metadata for a conversation, used in sidebar lists.
 */
export interface ConversationMeta {
  /** Unique identifier for the conversation. */
  id: string;
  /** Human-readable title, defaulting to "New Chat". */
  title: string;
  /** ISO timestamp of conversation creation. */
  created_at: string;
  /** ISO timestamp of last update. */
  updated_at: string;
}

/**
 * Full conversation detail including all messages.
 *
 * Extends ``ConversationMeta`` with the complete message history in its stored
 * (wire) form.
 */
export interface ConversationDetail extends ConversationMeta {
  /** Ordered list of messages in this conversation. */
  messages: StoredMessage[];
}

/**
 * The top-level response shape returned by ``POST /api/v1/chat``.
 */
export interface ChatResponse {
  /** The agent's generated response text. */
  response: string;
  /** List of source citations (policy section strings) for RAG-grounded answers. */
  sources: string[];
  /** Trace of tool calls made during response generation. */
  tool_calls: ToolCall[];
  /**
   * Backend-assigned id of the persisted assistant answer. Present when the
   * turn was persisted; this is the id the feedback endpoint accepts.
   */
  message_id?: string;
  /**
   * Backend-assigned id of the persisted user message. Present when the turn
   * was persisted.
   */
  user_message_id?: string;
}

/** Whether a streaming turn finished or failed. */
export type StreamStatus = "ok" | "error";

/**
 * What the assistant is doing while a turn is in flight.
 *
 * The phases are what the UI can honestly claim before any text has streamed:
 * the only signals available are the tool activity and the first delta.
 */
export type StreamPhase = "idle" | "searching" | "preparing" | "responding";

/** SSE event carrying one fragment of the assistant's streaming answer. */
export interface StreamTextDeltaEvent {
  type: "text_delta";
  content: string;
}

/** SSE event announcing that the agent invoked a tool. */
export interface StreamToolCallEvent {
  type: "tool_call";
  tool: string;
  arguments: Record<string, unknown>;
}

/** SSE event carrying the value a tool returned. */
export interface StreamToolResultEvent {
  type: "tool_result";
  tool: string;
  result: Record<string, unknown>;
}

/** SSE event carrying a tool failure reported through the model loop. */
export interface StreamToolErrorEvent {
  type: "tool_error";
  tool: string;
  error: string;
}

/** SSE event reporting that the model stream failed mid-response. */
export interface StreamErrorEvent {
  type: "error";
  message: string;
  code: string;
}

/**
 * SSE event that terminates every streaming turn.
 *
 * ``sources`` stays loosely typed because the backend serialises whatever the
 * agent collected rather than narrowing a citation into a stream failure.
 */
export interface StreamResponseCompleteEvent {
  type: "response_complete";
  sources: unknown[];
  status: StreamStatus;
}

/** Every typed frame the streaming endpoint can emit. */
export type StreamEvent =
  | StreamTextDeltaEvent
  | StreamToolCallEvent
  | StreamToolResultEvent
  | StreamToolErrorEvent
  | StreamErrorEvent
  | StreamResponseCompleteEvent;

/**
 * Generic SSE event payload from the ADK streaming endpoint.
 *
 * The backend emits ADK event JSON objects alongside the typed frames; those
 * envelopes carry no ``type`` discriminator we understand, so consumers must
 * treat them as opaque rather than assume a field is missing by mistake.
 */
export interface ADKSSEEvent {
  [key: string]: unknown;
}

/**
 * Names of the agent tools that act on claims rather than the policy corpus.
 *
 * These must match the names registered in ``backend/app/agent/registry.py``
 * exactly: a stale entry silently stops matching, and a missing one makes the
 * loading phase describe claim preparation as a policy search. The registered
 * claim tools are ``get_claim_status``, ``search_claims`` and
 * ``prepare_claim_submission``; ``query_policy`` is deliberately absent because
 * it reads the corpus, not a claim.
 *
 * The names are read from ``@/lib/api``, which declares them once next to the
 * claim confirmation client, so this set cannot drift from the constants the
 * UI components branch on.
 */
const CLAIM_TOOL_NAMES = new Set([
  GET_CLAIM_STATUS_TOOL,
  SEARCH_CLAIMS_TOOL,
  PREPARE_CLAIM_SUBMISSION_TOOL,
]);

/**
 * Whether a tool invocation is part of preparing or looking up a claim.
 *
 * The loading state uses this to tell the user which kind of work is in
 * flight, which is the only signal available before any text streams.
 *
 * @param tool - Registered tool name from a ``tool_call`` event.
 * @returns True for claim-oriented tools, false for corpus tools.
 */
export function isClaimTool(tool: string): boolean {
  return CLAIM_TOOL_NAMES.has(tool);
}

/**
 * The epoch is used as the sentinel for a stored message that arrived without
 * a usable timestamp; rendering "Jan 1, 1970" would be a lie, so such values
 * are treated as unknown.
 */
const UNKNOWN_TIMESTAMP_MS = 0;

/** Whether a timestamp is real enough to render. */
function isRenderable(date: Date): boolean {
  const time = date.getTime();
  return Number.isFinite(time) && time > UNKNOWN_TIMESTAMP_MS;
}

/**
 * True when both instants fall on the same calendar day in the viewer's
 * timezone.
 *
 * An unreadable timestamp never introduces a separator, because a malformed
 * row is not evidence that the day changed.
 *
 * @param a - First instant.
 * @param b - Second instant.
 * @returns Whether the two instants share a calendar day.
 */
export function isSameDay(a: Date, b: Date): boolean {
  if (!isRenderable(a) || !isRenderable(b)) return true;
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/**
 * Absolute timestamp for a chat message.
 *
 * Today's messages show only the clock, because the date is obvious from
 * context; anything older carries its date so the transcript stays readable
 * across days.
 *
 * @param date - When the message was created.
 * @returns Formatted timestamp, or an empty string when unknown.
 */
export function formatMessageTimestamp(date: Date): string {
  if (!isRenderable(date)) return "";
  const time = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "numeric",
    hour12: true,
  }).format(date);
  if (isSameDay(date, new Date())) return time;

  const day = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
  return `${day} at ${time}`;
}

/**
 * Label for a separator row placed between messages on different days.
 *
 * @param date - The day the following message belongs to.
 * @returns "Today", "Yesterday", or an absolute date; empty when unknown.
 */
export function formatDaySeparator(date: Date): string {
  if (!isRenderable(date)) return "";
  const today = new Date();
  if (isSameDay(date, today)) return "Today";

  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (isSameDay(date, yesterday)) return "Yesterday";

  return new Intl.DateTimeFormat(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
}

/**
 * Compact relative timestamp for the conversation list.
 *
 * Relative labels are only useful while they are short, so anything older than
 * a week falls back to the same absolute date the transcript uses.
 *
 * @param date - When the conversation was last updated.
 * @param now - Reference "now", injected so callers keep one clock per render.
 * @returns Relative or absolute label, or an empty string when unknown.
 */
export function formatRelativeTimestamp(date: Date, now: Date): string {
  if (!isRenderable(date)) return "";
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays}d ago`;

  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
}

/**
 * Coerce the loosely typed ``sources`` array of a ``response_complete`` event
 * into citations.
 *
 * The backend types that array as a list of anything, so anything that is
 * neither a string nor a citation object is dropped rather than rendered as
 * ``[object Object]``.
 *
 * @param value - Raw ``sources`` value from the event.
 * @returns Normalised citations in arrival order.
 */
export function normalizeCitations(value: unknown): SourceCitation[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is SourceCitation =>
      typeof item === "string" ||
      (typeof item === "object" &&
        item !== null &&
        typeof (item as { title?: unknown }).title === "string"),
  );
}

/**
 * Split a citation into the pieces the source pill UI needs.
 *
 * @param source - Citation as stored on the message.
 * @returns The pill label, its ``title`` text, and an excerpt when present.
 */
export function describeCitation(source: SourceCitation): CitationSummary {
  if (typeof source === "string") {
    return { label: source, title: source };
  }
  const { title, excerpt, section } = source;
  return {
    label: title,
    title: section ? `${title} — ${section}` : title,
    excerpt,
  };
}
