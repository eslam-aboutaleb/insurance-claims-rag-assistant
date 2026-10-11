import { z } from "zod";

export const ChatResponseSchema = z.object({
  response: z.string(),
  sources: z.array(z.string()),
  tool_calls: z.array(
    z.object({
      name: z.string(),
      arguments: z.record(z.string(), z.unknown()),
      result: z.unknown().optional(),
    }),
  ),
});

export const ConversationMetaSchema = z.object({
  id: z.string(),
  title: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
});

/**
 * A citation is usually a plain string; when the retrieval layer provides the
 * chunk it read, it arrives as an object with an excerpt.
 */
const SourceCitationSchema = z.union([
  z.string(),
  z.object({
    title: z.string(),
    excerpt: z.string().optional(),
    section: z.string().optional(),
  }),
]);

export const ConversationDetailSchema = z.object({
  id: z.string(),
  title: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
  messages: z.array(
    z.object({
      id: z.string(),
      role: z.enum(["user", "assistant", "error"]),
      content: z.string(),
      timestamp: z.string(),
      error: z.string().optional(),
      sources: z.array(SourceCitationSchema).optional(),
      tool_calls: z
        .array(
          z.object({
            name: z.string(),
            arguments: z.record(z.string(), z.unknown()),
            result: z.unknown().optional(),
          }),
        )
        .optional(),
    }),
  ),
});

export const UserMeSchema = z.object({
  user_id: z.string(),
});

// --- Streaming event schemas ----------------------------------------------
//
// These mirror the Pydantic ``Stream*`` models the backend emits on
// ``POST /api/v1/chat/stream``. They exist so the frontend has to parse a
// frame before trusting it: an event that fails validation is an ADK envelope
// we do not understand, not a malformed one to surface as an error.

export const StreamTextDeltaSchema = z.object({
  type: z.literal("text_delta"),
  content: z.string(),
});

export const StreamToolCallSchema = z.object({
  type: z.literal("tool_call"),
  tool: z.string(),
  arguments: z.record(z.string(), z.unknown()),
});

export const StreamToolResultSchema = z.object({
  type: z.literal("tool_result"),
  tool: z.string(),
  result: z.record(z.string(), z.unknown()),
});

export const StreamToolErrorSchema = z.object({
  type: z.literal("tool_error"),
  tool: z.string(),
  error: z.string(),
});

export const StreamErrorSchema = z.object({
  type: z.literal("error"),
  message: z.string(),
  code: z.string(),
});

/**
 * The terminal frame of every turn.
 *
 * ``sources`` is deliberately loose — the backend serialises whatever the
 * agent collected — and ``status`` defaults to ``"ok"`` so an older backend
 * that omits it still parses.
 */
export const StreamResponseCompleteSchema = z.object({
  type: z.literal("response_complete"),
  sources: z.array(z.unknown()),
  status: z.enum(["ok", "error"]).default("ok"),
});

/**
 * Every typed frame, discriminated on ``type``.
 *
 * @param value - Decoded SSE payload.
 * @returns The parsed event, or ``null`` when the payload is not one of ours.
 */
export function parseStreamEvent(value: unknown) {
  const result = z
    .discriminatedUnion("type", [
      StreamTextDeltaSchema,
      StreamToolCallSchema,
      StreamToolResultSchema,
      StreamToolErrorSchema,
      StreamErrorSchema,
      StreamResponseCompleteSchema,
    ])
    .safeParse(value);
  return result.success ? result.data : null;
}
