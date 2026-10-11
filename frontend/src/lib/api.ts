/**
 * API client functions for communicating with the OmniCare FastAPI backend.
 *
 * This module centralizes all HTTP requests to the backend, including
 * authentication header injection, idempotency key generation, and error
 * normalization. Components should import from here rather than calling
 * ``fetch`` directly to ensure consistent request handling.
 *
 * SSE event contract:
 *   ``streamMessage`` forwards every typed frame from ``POST /chat/stream``.
 *   Each frame is JSON with a ``type`` discriminator: ``text_delta``,
 *   ``tool_call``, ``tool_result``, ``tool_error``, ``error``, and the
 *   terminal ``response_complete`` (whose ``status`` is ``"ok"`` or
 *   ``"error"``). A frame that is not parseable JSON is surfaced through
 *   ``onError`` rather than dropped, so a truncated stream can never look
 *   like a completed one.
 *
 * Environment:
 *   The backend URL is read from ``VITE_API_URL``. In development,
 *   this defaults to ``http://localhost:8000``. In production, set this to
 *   the deployed backend origin.
 */

import { ChatResponse, ADKSSEEvent, ToolCall } from "@/types/chat";
import {
  ChatResponseSchema,
  ConversationMetaSchema,
  ConversationDetailSchema,
  UserMeSchema,
} from "@/lib/validation";
import { z } from "zod";

/** Whether a streaming turn finished or failed. */
export type StreamStatus = "ok" | "error";

/** The agent invoked a tool. */
export interface StreamToolCallEvent {
  type: "tool_call";
  /** The registered name of the tool the agent invoked. */
  tool: string;
  /** The arguments the agent passed to the tool. */
  arguments: Record<string, unknown>;
}

/** A tool returned a value. */
export interface StreamToolResultEvent {
  type: "tool_result";
  /** The registered name of the tool that returned. */
  tool: string;
  /** The value the tool returned. */
  result: Record<string, unknown>;
}

/** A tool reported its own failure instead of returning a value. */
export interface StreamToolErrorEvent {
  type: "tool_error";
  /** The registered name of the tool that failed. */
  tool: string;
  /** The failure description the tool reported. */
  error: string;
}

/** The model stream failed. Always followed by a terminal ``response_complete``. */
export interface StreamErrorEvent {
  type: "error";
  /** User-facing description of the failure. */
  message: string;
  /** Machine-readable failure code, e.g. ``"stream_failed"``. */
  code: string;
}

/**
 * Optional callbacks for the typed stream events.
 *
 * Each callback is invoked in addition to (never instead of) the positional
 * ``onChunk`` handler, so consumers that only understand the original
 * ``text_delta``/``response_complete`` frames keep working unchanged.
 */
export interface StreamMessageHandlers {
  /** A fragment of the assistant answer arrived. */
  onTextDelta?: (content: string) => void;
  /** The agent invoked a tool. */
  onToolCall?: (event: StreamToolCallEvent) => void;
  /** A tool returned a value. */
  onToolResult?: (event: StreamToolResultEvent) => void;
  /** A tool reported its own failure. */
  onToolError?: (event: StreamToolErrorEvent) => void;
  /** The model stream failed mid-answer. */
  onError?: (event: StreamErrorEvent) => void;
  /** The stream terminated. ``status`` distinguishes success from failure. */
  onResponseComplete?: (sources: string[], status: StreamStatus) => void;
}

const MALFORMED_EVENT_CODE = "malformed_event";

/**
 * A stream that stopped before its terminal frame.
 *
 * Kept distinct from ``MALFORMED_EVENT_CODE`` because the two are different
 * failures in telemetry: a truncated turn is a server or transport problem,
 * while a malformed event is a frame this client could not read.
 */
const INCOMPLETE_STREAM_CODE = "incomplete_stream";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

/**
 * Read a decoded SSE payload as a tool invocation, filling in defaults for
 * fields the server omitted rather than trusting the raw cast.
 */
function asToolCallEvent(event: ADKSSEEvent): StreamToolCallEvent {
  return {
    type: "tool_call",
    tool: asString(event.tool),
    arguments: asRecord(event.arguments),
  };
}

function asToolResultEvent(event: ADKSSEEvent): StreamToolResultEvent {
  return {
    type: "tool_result",
    tool: asString(event.tool),
    result: asRecord(event.result),
  };
}

function asToolErrorEvent(event: ADKSSEEvent): StreamToolErrorEvent {
  return {
    type: "tool_error",
    tool: asString(event.tool),
    error: asString(event.error),
  };
}

function asErrorEvent(event: ADKSSEEvent): StreamErrorEvent {
  return {
    type: "error",
    message: asString(event.message),
    code: asString(event.code),
  };
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function getApiUrl(): string {
  return import.meta.env.VITE_API_URL || "http://localhost:8000";
}

function generateIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
}

async function parseResponse<T>(
  response: Response,
  schema: (data: unknown) => T,
): Promise<T> {
  const data = await response.json();
  if (response.ok) {
    return schema(data);
  }

  let errorMessage = "Something went wrong. Please try again later.";
  let errorCode: string | undefined;

  try {
    if (data?.error?.message) {
      errorMessage = data.error.message;
    } else if (data?.detail) {
      errorMessage = data.detail;
    }
    errorCode = data?.error?.code;
  } catch {
    // Body was not JSON
  }

  if (response.status === 401) {
    errorMessage = "Session expired. Please log in again.";
    errorCode = "UNAUTHORIZED";
  }

  throw new ApiError(errorMessage, response.status, errorCode);
}

function buildAuthHeaders(token: string | null): Record<string, string> {
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

/**
 * Send a chat message to the OmniCare backend.
 */
export async function sendMessage(
  token: string | null,
  message: string,
  signal?: AbortSignal,
  idempotencyKey?: string,
): Promise<ChatResponse> {
  const apiUrl = getApiUrl();
  const key = idempotencyKey ?? generateIdempotencyKey();

  try {
    const response = await fetch(`${apiUrl}/api/v1/chat`, {
      method: "POST",
      credentials: "include",
      headers: {
        ...buildAuthHeaders(token),
        "Idempotency-Key": key,
      },
      body: JSON.stringify({ message }),
      signal,
    });

    if (response.ok) {
      return parseResponse(response, ChatResponseSchema.parse);
    }
    throw await parseResponse(response, (d) => d);
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "Something went wrong. Please try again later.",
      0,
      "NETWORK_ERROR",
    );
  }
}

/**
 * Reset the current user's chat session on the backend.
 */
export async function resetChat(token: string | null): Promise<void> {
  const apiUrl = getApiUrl();

  try {
    const response = await fetch(`${apiUrl}/api/v1/chat/reset`, {
      method: "POST",
      credentials: "include",
      headers: buildAuthHeaders(token),
    });

    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new ApiError(
        data?.detail ||
          data?.error?.message ||
          "Something went wrong. Please try again later.",
        response.status,
      );
    }
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "Something went wrong. Please try again later.",
      0,
      "NETWORK_ERROR",
    );
  }
}

/**
 * Fetch the list of past conversations for the authenticated user.
 */
export async function getConversations(token: string, signal?: AbortSignal) {
  const apiUrl = getApiUrl();

  try {
    const response = await fetch(`${apiUrl}/api/v1/chat/conversations`, {
      credentials: "include",
      headers: { Authorization: `Bearer ${token}` },
      signal,
    });

    if (response.ok) {
      return parseResponse(response, (d) => ({
        conversations: z
          .array(ConversationMetaSchema)
          .parse((d as { conversations?: unknown }).conversations ?? []),
      }));
    }
    throw await parseResponse(response, (d) => d);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "Something went wrong. Please try again later.",
      0,
      "NETWORK_ERROR",
    );
  }
}

/**
 * Fetch the full message history for a specific conversation.
 */
export async function getConversationHistory(
  token: string,
  id: string,
  signal?: AbortSignal,
) {
  const apiUrl = getApiUrl();

  try {
    const response = await fetch(`${apiUrl}/api/v1/chat/conversations/${id}`, {
      credentials: "include",
      headers: { Authorization: `Bearer ${token}` },
      signal,
    });

    if (response.ok) {
      return parseResponse(response, ConversationDetailSchema.parse);
    }
    throw await parseResponse(response, (d) => d);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "Something went wrong. Please try again later.",
      0,
      "NETWORK_ERROR",
    );
  }
}

/**
 * Validate the current session by calling /api/v1/auth/me.
 */
export async function getCurrentUser(
  token: string | null,
  signal?: AbortSignal,
) {
  const apiUrl = getApiUrl();

  try {
    const response = await fetch(`${apiUrl}/api/v1/auth/me`, {
      method: "GET",
      credentials: "include",
      headers: buildAuthHeaders(token),
      signal,
    });

    if (response.status === 204) {
      return null;
    }

    if (response.ok) {
      return parseResponse(response, UserMeSchema.parse);
    }
    throw await parseResponse(response, (d) => d);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "Something went wrong. Please try again later.",
      0,
      "NETWORK_ERROR",
    );
  }
}

/**
 * Route one decoded SSE payload to the generic chunk handler and to any
 * typed callback that matches it.
 *
 * Frames whose type is not part of the contract are still handed to
 * ``onChunk``: the backend forwards raw ADK envelopes alongside the typed
 * events, and those are not failures. A payload that is not JSON at all is
 * handled before it reaches here, so it can never be dropped silently.
 *
 * @returns Whether this payload was the terminal ``response_complete``.
 */
function dispatchStreamEvent(
  data: unknown,
  onChunk: (eventData: ADKSSEEvent) => void,
  handlers: StreamMessageHandlers,
): boolean {
  if (!isRecord(data)) {
    console.error("Ignoring malformed SSE payload:", data);
    handlers.onError?.(MALFORMED_STREAM_ERROR);
    return false;
  }

  const event = data as ADKSSEEvent;
  switch (event.type as string | undefined) {
    case "text_delta":
      onChunk(event);
      handlers.onTextDelta?.(asString(event.content));
      return false;
    case "tool_call":
      onChunk(event);
      handlers.onToolCall?.(asToolCallEvent(event));
      return false;
    case "tool_result":
      onChunk(event);
      handlers.onToolResult?.(asToolResultEvent(event));
      return false;
    case "tool_error":
      onChunk(event);
      handlers.onToolError?.(asToolErrorEvent(event));
      return false;
    case "error":
      onChunk(event);
      handlers.onError?.(asErrorEvent(event));
      return false;
    case "response_complete":
      onChunk(event);
      handlers.onResponseComplete?.(
        asStringArray(event.sources),
        event.status === "error" ? "error" : "ok",
      );
      return true;
    default:
      onChunk(event);
      return false;
  }
}

const MALFORMED_STREAM_ERROR: StreamErrorEvent = {
  type: "error",
  message: "The server sent a malformed event. The answer may be incomplete.",
  code: MALFORMED_EVENT_CODE,
};

const INCOMPLETE_STREAM_ERROR: StreamErrorEvent = {
  type: "error",
  message: "The response ended unexpectedly. The answer may be incomplete.",
  code: INCOMPLETE_STREAM_CODE,
};

/**
 * Stream a chat message and emit incremental text deltas.
 *
 * ``onChunk`` still receives every event the backend sends, so existing
 * consumers of the ``text_delta`` and ``response_complete`` frames keep
 * working unchanged. Pass ``handlers`` to also receive the tool activity and
 * error frames added by the streaming contract.
 */
export async function streamMessage(
  token: string | null,
  message: string,
  onChunk: (eventData: ADKSSEEvent) => void,
  signal?: AbortSignal,
  idempotencyKey?: string,
  handlers?: StreamMessageHandlers,
): Promise<void> {
  const apiUrl = getApiUrl();
  const key = idempotencyKey ?? generateIdempotencyKey();

  /** Decode and route one ``data:`` payload, never dropping a bad one silently. */
  const dispatch = (raw: string): boolean => {
    try {
      return dispatchStreamEvent(JSON.parse(raw), onChunk, handlers ?? {});
    } catch (e) {
      console.error("Error parsing SSE chunk:", e);
      handlers?.onError?.(MALFORMED_STREAM_ERROR);
      return false;
    }
  };

  try {
    const response = await fetch(`${apiUrl}/api/v1/chat/stream`, {
      method: "POST",
      credentials: "include",
      headers: {
        ...buildAuthHeaders(token),
        "Idempotency-Key": key,
      },
      body: JSON.stringify({ message }),
      signal,
    });

    if (!response.ok) {
      throw await parseResponse(response, (d) => d);
    }

    const reader = response.body?.getReader();
    if (!reader)
      throw new ApiError("No readable stream available", 0, "NETWORK_ERROR");

    const decoder = new TextDecoder();
    let buffer = "";
    let sawTerminalEvent = false;
    let isDone = false;

    while (!isDone) {
      const { done, value } = await reader.read();
      isDone = done;
      if (done) {
        // A final frame can arrive without its trailing blank line.
        if (buffer.startsWith("data: ")) {
          sawTerminalEvent = dispatch(buffer.slice(6)) || sawTerminalEvent;
        }
        break;
      }

      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const chunk = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);

        if (chunk.startsWith("data: ")) {
          sawTerminalEvent = dispatch(chunk.slice(6)) || sawTerminalEvent;
        }

        boundary = buffer.indexOf("\n\n");
      }
    }

    if (!sawTerminalEvent) {
      // Without a terminal frame the stream died mid-answer, which the UI
      // would otherwise render as a complete response.
      console.error("Stream ended without a terminal response_complete event");
      handlers?.onError?.(INCOMPLETE_STREAM_ERROR);
    }
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "Something went wrong. Please try again later.",
      0,
      "NETWORK_ERROR",
    );
  }
}

// --- Claim confirmation client ----------------------------------------------

/**
 * Two-step claim submission.
 *
 * The agent stages a claim with the ``prepare_claim_submission`` tool, which
 * returns a single-use confirmation token; nothing is written until a user
 * confirms in the UI. These two calls are therefore the only path from a
 * prepared claim to a filed one, and neither may be reached by a tool.
 *
 * Contract:
 *   ``POST /claims/confirm`` with ``{"confirmation_token": token}`` answers
 *   ``{success, claim_id, status, message}``. A token that is invalid, expired,
 *   or already used is a 400 whose ``detail`` says so, and a 200 carrying
 *   ``success: false`` means the submission itself was rejected.
 *   ``POST /claims/cancel`` with the same body retires a pending submission
 *   before it expires. A deployment that has not landed the endpoint answers
 *   404 or 501, which is a degraded cancel rather than a failure: the pending
 *   submission is simply left to expire.
 */

/**
 * Registry names of the agent's claim tools, declared in one place.
 *
 * ``backend/app/agent/registry.py`` is the authority for these strings; a name
 * that drifts from it silently stops matching, which is why the claim tool set
 * in ``types/chat.ts`` and every component that branches on a tool name reads
 * them from here instead of repeating a literal.
 */

/** The tool that reports the status of one existing claim. */
export const GET_CLAIM_STATUS_TOOL = "get_claim_status";

/** The tool that lists the caller's claims. */
export const SEARCH_CLAIMS_TOOL = "search_claims";

/** The agent tool that stages a claim for confirmation. */
export const PREPARE_CLAIM_SUBMISSION_TOOL = "prepare_claim_submission";

/** The claim a user is being asked to confirm. */
export interface PreparedClaimData {
  /** The policy the claim is filed against, e.g. ``"POL-1092"``. */
  policy_number: string;
  /** Category of the claim, e.g. ``"Water Damage"``. */
  claim_type: string;
  /** Claimed amount in dollars. */
  amount: number;
  /** Factual description of the incident. */
  description: string;
}

/**
 * A prepared claim awaiting the user's decision, held on the tool call that
 * staged it.
 *
 * Storing the joined halves as structured state is what lets the card render
 * the values under review without re-reading them out of a serialised string
 * that has already drifted from what the agent sent.
 */
export interface PendingClaimConfirmation {
  /** Discriminator, so this is distinguishable from any other tool result. */
  kind: "pending_claim_confirmation";
  /** Single-use token presented to the confirm endpoint. */
  token: string;
  /** ISO timestamp after which the token is refused. */
  expiresAt: string;
  /** Backend status of the pending submission. */
  status: string;
  /** The tool's own instruction to confirm in the UI. */
  message: string;
  /** The values being confirmed. */
  claimData: PreparedClaimData;
}

/** The claim that now exists because a confirmation succeeded. */
export interface ClaimConfirmationReceipt {
  /** Discriminator matching the stored tool-call outcome. */
  kind: "claim_confirmation_receipt";
  /** Human-readable claim identifier, e.g. ``"CLM-8821"``. */
  claimId: string;
  /** Status the claim was filed with. */
  status: string;
  /** Confirmation message returned by the backend. */
  message: string;
}

/**
 * Why a confirmation can no longer be completed.
 *
 * ``expired`` and ``unusable`` are terminal: the token is spent, so the only
 * way forward is to ask the assistant again. ``retryable`` is a transport or
 * server failure, which leaves the token exactly as usable as it was.
 */
export type ClaimFailureKind = "expired" | "unusable" | "retryable";

/** A confirmation attempt that did not produce a claim. */
export interface ClaimFailure {
  /** Which recovery path the card should offer. */
  kind: ClaimFailureKind;
  /** User-facing explanation, taken from the backend where there is one. */
  message: string;
}

/** What a cancellation attempt resolved to. */
export interface ClaimCancellationResult {
  /** Whether the backend retired the pending submission. */
  cancelled: boolean;
  /** User-facing explanation of what happened instead when it did not. */
  message: string;
}

/** Terminal record stored on a tool call once the user has cancelled. */
export interface ClaimCancellationNote {
  /** Discriminator matching the stored tool-call outcome. */
  kind: "claim_cancellation_note";
  /** False when the cancel endpoint is absent and the token expires instead. */
  cancelled: boolean;
  /** Explanation shown to the user. */
  message: string;
}

/** The fields a receipt shows when it reads a confirmed claim back. */
export interface ClaimDetailView {
  /** Human-readable claim identifier. */
  claim_id: string;
  /** The policy the claim was filed against. */
  policy_number?: string;
  /** Category of the claim. */
  claim_type?: string;
  /** Current processing status. */
  status?: string;
  /** Claimed amount, as either a number or a decimal string. */
  amount?: number | string;
  /** The claim's description. */
  description?: string;
}

/** The confirmed claim, as the confirm endpoint reports it. */
export interface ClaimConfirmationResponse {
  /** Whether the backend accepted the confirmation and filed the claim. */
  success: boolean;
  /** Human-readable claim identifier, absent when the submission failed. */
  claim_id?: string;
  /** Status the claim was filed with, absent when the submission failed. */
  status?: string;
  /** Confirmation message returned by the backend. */
  message: string;
}

/** A cancellation the deployment could not perform. */
export class ClaimCancelUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ClaimCancelUnavailableError";
  }
}

const CANCEL_UNAVAILABLE_MESSAGE =
  "Cancelling is not available on this deployment, so the prepared claim was left as it is. " +
  "It expires on its own within 15 minutes, and no claim is filed unless you confirm.";

function asClaimTextField(
  args: Record<string, unknown>,
  field: string,
): string {
  const value = args[field];
  return typeof value === "string" ? value : "";
}

/** Amounts cross the wire as either a JSON number or a decimal string. */
function asClaimAmount(args: Record<string, unknown>): number {
  const raw = args.amount;
  const numeric = typeof raw === "string" ? Number(raw) : raw;
  return typeof numeric === "number" && Number.isFinite(numeric) ? numeric : 0;
}

/**
 * Join a ``prepare_claim_submission`` result with the arguments that produced
 * it into the structured state the confirmation card renders.
 *
 * The claim fields travel on the invocation's arguments and the token and
 * expiry on its result, so neither half is sufficient on its own.
 *
 * @param call - The tool call whose arguments carry the claim fields.
 * @param result - The value the tool returned.
 * @returns The prepared claim, or ``null`` for any other tool or a prepare
 *   that never produced a token.
 */
export function toPendingClaimConfirmation(
  call: ToolCall,
  result: unknown,
): PendingClaimConfirmation | null {
  if (call.name !== PREPARE_CLAIM_SUBMISSION_TOOL || !isRecord(result))
    return null;
  const token = asString(result.confirmation_token);
  if (!token) return null;

  return {
    kind: "pending_claim_confirmation",
    token,
    expiresAt: asString(result.expires_at),
    status: asString(result.status) || "pending",
    message: asString(result.message),
    claimData: {
      policy_number: asClaimTextField(call.arguments, "policy_number"),
      claim_type: asClaimTextField(call.arguments, "claim_type"),
      amount: asClaimAmount(call.arguments),
      description: asClaimTextField(call.arguments, "description"),
    },
  };
}

/**
 * Read the prepared claim stored on a tool call, if it is still awaiting a
 * decision.
 *
 * @param value - The tool call's stored result.
 * @returns The prepared claim, or ``null`` for any other outcome.
 */
export function asPendingClaimConfirmation(
  value: unknown,
): PendingClaimConfirmation | null {
  if (!isRecord(value) || value.kind !== "pending_claim_confirmation")
    return null;
  const token = asString(value.token);
  if (!token) return null;

  const claimData = asRecord(value.claimData);
  const amount =
    typeof claimData.amount === "number"
      ? claimData.amount
      : Number(claimData.amount);
  return {
    kind: "pending_claim_confirmation",
    token,
    expiresAt: asString(value.expiresAt),
    status: asString(value.status) || "pending",
    message: asString(value.message),
    claimData: {
      policy_number: asString(claimData.policy_number),
      claim_type: asString(claimData.claim_type),
      amount: Number.isFinite(amount) ? amount : 0,
      description: asString(claimData.description),
    },
  };
}

/**
 * Read the receipt stored on a tool call after a successful confirmation.
 *
 * @param value - The tool call's stored result.
 * @returns The receipt, or ``null`` for any other outcome.
 */
export function asClaimConfirmationReceipt(
  value: unknown,
): ClaimConfirmationReceipt | null {
  if (!isRecord(value) || value.kind !== "claim_confirmation_receipt")
    return null;
  return {
    kind: "claim_confirmation_receipt",
    claimId: asString(value.claimId),
    status: asString(value.status),
    message: asString(value.message),
  };
}

/**
 * Read the cancellation note stored on a tool call after the user cancelled.
 *
 * @param value - The tool call's stored result.
 * @returns The note, or ``null`` for any other outcome.
 */
export function asClaimCancellationNote(
  value: unknown,
): ClaimCancellationNote | null {
  if (!isRecord(value) || value.kind !== "claim_cancellation_note") return null;
  return {
    kind: "claim_cancellation_note",
    cancelled: value.cancelled !== false,
    message: asString(value.message),
  };
}

function asClaimConfirmationResponse(data: unknown): ClaimConfirmationResponse {
  const record = asRecord(data);
  return {
    success: record.success === true,
    claim_id: asString(record.claim_id) || undefined,
    status: asString(record.status) || undefined,
    message: asString(record.message),
  };
}

function asClaimCancellationResult(data: unknown): ClaimCancellationResult {
  const record = asRecord(data);
  return {
    cancelled: record.success !== false,
    message:
      asString(record.message) ||
      "The prepared claim was cancelled. No claim was filed.",
  };
}

function asClaimDetailView(data: unknown): ClaimDetailView {
  const record = asRecord(data);
  return {
    claim_id: asString(record.claim_id),
    policy_number: asString(record.policy_number) || undefined,
    claim_type: asString(record.claim_type) || undefined,
    status: asString(record.status) || undefined,
    amount:
      typeof record.amount === "number" || typeof record.amount === "string"
        ? record.amount
        : undefined,
    description: asString(record.description) || undefined,
  };
}

/**
 * Confirm a prepared claim and file it.
 *
 * @param token - The authenticated user's bearer token.
 * @param confirmationToken - Token issued when the claim was prepared.
 * @param signal - Optional abort signal for the request.
 * @returns The confirmation outcome, including the claim id on success.
 * @throws ApiError On a refused token, a rejected submission, or a transport
 *   failure. A 400 with ``detail`` "Invalid, expired, or already used
 *   confirmation token." means the token is spent.
 */
export async function confirmClaim(
  token: string | null,
  confirmationToken: string,
  signal?: AbortSignal,
): Promise<ClaimConfirmationResponse> {
  const apiUrl = getApiUrl();

  try {
    const response = await fetch(`${apiUrl}/api/v1/claims/confirm`, {
      method: "POST",
      credentials: "include",
      headers: buildAuthHeaders(token),
      body: JSON.stringify({ confirmation_token: confirmationToken }),
      signal,
    });

    if (response.ok) {
      return parseResponse(response, asClaimConfirmationResponse);
    }
    throw await parseResponse(response, (d) => d);
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "We could not reach the server, so your claim has not been filed yet.",
      0,
      "NETWORK_ERROR",
    );
  }
}

/**
 * Retire a prepared claim before its token expires.
 *
 * Degrades gracefully on a deployment that has not landed the endpoint: the
 * caller is told the submission will expire on its own instead of being told
 * the cancellation succeeded.
 *
 * @param token - The authenticated user's bearer token.
 * @param confirmationToken - Token issued when the claim was prepared.
 * @param signal - Optional abort signal for the request.
 * @returns The cancellation outcome.
 * @throws ClaimCancelUnavailableError When the endpoint is absent, so the
 *   submission is left to expire.
 * @throws ApiError On any other refusal or transport failure, which leaves the
 *   pending submission untouched.
 */
export async function cancelClaimSubmission(
  token: string | null,
  confirmationToken: string,
  signal?: AbortSignal,
): Promise<ClaimCancellationResult> {
  const apiUrl = getApiUrl();

  let response: Response;
  try {
    response = await fetch(`${apiUrl}/api/v1/claims/cancel`, {
      method: "POST",
      credentials: "include",
      headers: buildAuthHeaders(token),
      body: JSON.stringify({ confirmation_token: confirmationToken }),
      signal,
    });
  } catch (error) {
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "We could not reach the server, so nothing was cancelled. The prepared claim is still waiting.",
      0,
      "NETWORK_ERROR",
    );
  }

  // A missing cancel endpoint answers as a routing failure, which is a
  // deployment gap rather than a refused cancellation.
  if (response.status === 404 || response.status === 501) {
    throw new ClaimCancelUnavailableError(CANCEL_UNAVAILABLE_MESSAGE);
  }

  if (response.ok) {
    return parseResponse(response, asClaimCancellationResult);
  }
  throw await parseResponse(response, (d) => d);
}

/**
 * Read one confirmed claim back so a receipt can link to it.
 *
 * @param token - The authenticated user's bearer token.
 * @param claimId - The claim identifier the confirmation returned.
 * @param signal - Optional abort signal for the request.
 * @returns The claim's stored fields.
 * @throws ApiError When the claim cannot be read. The caller degrades to the
 *   receipt it already has, because the claim exists regardless.
 */
export async function lookupConfirmedClaim(
  token: string | null,
  claimId: string,
  signal?: AbortSignal,
): Promise<ClaimDetailView> {
  const apiUrl = getApiUrl();

  try {
    const response = await fetch(
      `${apiUrl}/api/v1/claims/${encodeURIComponent(claimId)}`,
      {
        credentials: "include",
        headers: buildAuthHeaders(token),
        signal,
      },
    );

    if (response.ok) {
      return parseResponse(response, asClaimDetailView);
    }
    throw await parseResponse(response, (d) => d);
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "Something went wrong. Please try again later.",
      0,
      "NETWORK_ERROR",
    );
  }
}

// --- Answer feedback client ------------------------------------------------

/**
 * Whether a user found an assistant answer helpful.
 *
 * Mirrors the backend's rating enum, so a value that is not one of these two
 * is never sent and never stored.
 */
export type FeedbackRating = "up" | "down";

/**
 * Why a user rated an answer unhelpful.
 *
 * Mirrors the backend's category enum; the structured values are what make two
 * users' reasons countable against each other.
 */
export type FeedbackCategory =
  | "wrong_answer"
  | "outdated_policy"
  | "unhelpful_tone"
  | "missing_info"
  | "other";

/** The rating as the feedback endpoint stores it. */
export interface AnswerFeedback {
  /** Primary key of the stored rating. */
  id: string;
  /** The rated conversation message. */
  message_id: string;
  /** The stored rating, revised in place when the user changed their mind. */
  rating: FeedbackRating;
  /** Free-text reason the user typed, when one was given. */
  comment?: string;
  /** Structured reason the user picked, when one was given. */
  category?: FeedbackCategory;
  /** When the row was written by the database. */
  created_at: string;
}

const FEEDBACK_CATEGORIES: readonly FeedbackCategory[] = [
  "wrong_answer",
  "outdated_policy",
  "unhelpful_tone",
  "missing_info",
  "other",
];

function asFeedbackRating(value: unknown): FeedbackRating {
  return value === "up" ? "up" : "down";
}

function asFeedbackCategory(value: unknown): FeedbackCategory | undefined {
  return FEEDBACK_CATEGORIES.find((category) => category === value);
}

function asAnswerFeedback(data: unknown): AnswerFeedback {
  const record = asRecord(data);
  const category = asFeedbackCategory(record.category);
  return {
    id: asString(record.id),
    message_id: asString(record.message_id),
    rating: asFeedbackRating(record.rating),
    comment: asString(record.comment) || undefined,
    ...(category ? { category } : {}),
    created_at: asString(record.created_at),
  };
}

/**
 * Record or revise the caller's rating on one assistant answer.
 *
 * Rating the same message twice revises the stored row rather than adding a
 * second one, so the caller does not have to withdraw a rating before changing
 * their mind. The comment and category are optional and only travel when the
 * caller gave them.
 *
 * @param token - The authenticated user's bearer token.
 * @param messageId - The conversation message being rated.
 * @param rating - Whether the answer was helpful.
 * @param comment - Optional free-text reason, capped to match the server.
 * @param category - Optional structured reason the answer was unhelpful.
 * @param signal - Optional abort signal for the request.
 * @returns The rating as it is now stored.
 * @throws ApiError On a message the caller does not own, a rejected body, or a
 *   transport failure. A 403 means the message exists but is not theirs, so the
 *   UI reverts the optimistic rating instead of retrying.
 */
export async function submitFeedback(
  token: string | null,
  messageId: string,
  rating: FeedbackRating,
  comment?: string,
  category?: FeedbackCategory,
  signal?: AbortSignal,
): Promise<AnswerFeedback> {
  const apiUrl = getApiUrl();

  try {
    const response = await fetch(`${apiUrl}/api/v1/feedback`, {
      method: "POST",
      credentials: "include",
      headers: buildAuthHeaders(token),
      body: JSON.stringify({
        message_id: messageId,
        rating,
        ...(comment ? { comment } : {}),
        ...(category ? { category } : {}),
      }),
      signal,
    });

    if (response.ok) {
      return parseResponse(response, asAnswerFeedback);
    }
    throw await parseResponse(response, (d) => d);
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "We could not reach the server, so your rating was not saved.",
      0,
      "NETWORK_ERROR",
    );
  }
}

/**
 * Withdraw the caller's rating on one assistant answer.
 *
 * Withdrawing is idempotent: a caller who never rated the message gets the same
 * empty success as one who just did, so the UI can always offer the thumbs back
 * without first knowing what the server holds.
 *
 * @param token - The authenticated user's bearer token.
 * @param messageId - The rated conversation message.
 * @param signal - Optional abort signal for the request.
 * @returns Nothing on success; the row is gone.
 * @throws ApiError On a message the caller does not own or a transport
 *   failure, which leaves the stored rating untouched.
 */
export async function deleteFeedback(
  token: string | null,
  messageId: string,
  signal?: AbortSignal,
): Promise<void> {
  const apiUrl = getApiUrl();

  try {
    const response = await fetch(
      `${apiUrl}/api/v1/feedback/${encodeURIComponent(messageId)}`,
      {
        method: "DELETE",
        credentials: "include",
        headers: buildAuthHeaders(token),
        signal,
      },
    );

    if (response.status === 204) {
      return;
    }
    throw await parseResponse(response, (d) => d);
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if ((error as Error).name === "AbortError") {
      throw new ApiError("Request cancelled", 0, "CANCELLED");
    }
    throw new ApiError(
      "We could not reach the server, so your rating is still saved.",
      0,
      "NETWORK_ERROR",
    );
  }
}
