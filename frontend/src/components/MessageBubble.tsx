/**
 * MessageBubble component.
 *
 * Renders a single chat message with distinct styling for user, assistant,
 * and error messages. Assistant messages support Markdown rendering with
 * syntax highlighting, source citation badges, and tool call transparency
 * badges.
 *
 * A mid-stream failure does not replace the assistant bubble: the partial
 * answer stays in place and the failure is appended beneath it as a note.
 *
 * An assistant answer also carries a thumbs-up / thumbs-down pair in its hover
 * action row. The rating is optimistic: the active state renders before the
 * server answers and reverts, with a non-blocking toast, if the request fails.
 * A thumbs-down additionally offers an optional reason and note, because a
 * rating that says nothing about why is far less useful to whoever is trying to
 * improve the answer.
 *
 * The rating lives in this component's own state, keyed by message id. It is
 * not persisted on the message or reloaded from the server, so it describes
 * this browsing session rather than the caller's history.
 *
 * Layout:
 *   - User messages are right-aligned with a blue background.
 *   - Assistant messages are left-aligned with a white background and
 *     include the bot avatar.
 *   - Error messages use a red-tinted background and error icon.
 */

import { useEffect, useRef, useState } from "react";
import { toast } from "react-hot-toast";
import { Message, formatMessageTimestamp } from "@/types/chat";
import {
  Bot,
  User,
  AlertCircle,
  Copy,
  Check,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  ThumbsUp,
  ThumbsDown,
} from "lucide-react";
import ReactMarkdown, { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import SourcesBadge from "./SourcesBadge";
import ToolCallBadge from "./ToolCallBadge";
import {
  asClaimCancellationNote,
  asClaimConfirmationReceipt,
  asPendingClaimConfirmation,
  deleteFeedback,
  submitFeedback,
  type FeedbackCategory,
  type FeedbackRating,
} from "@/lib/api";

interface MessageBubbleProps {
  /** The message to render. */
  message: Message;
  /** Callback to retry the last user message. */
  onRetry?: () => void;
  /**
   * Render an assistant turn that has said nothing yet without the empty
   * bubble shell: no bubble, no hover row, just the tool trace, citations and
   * feedback chrome it carries. A stream that has only begun shows a bubble for
   * a fraction of a second before the first token arrives, and that flash is
   * what this suppresses.
   */
  bare?: boolean;
  /**
   * Put a thin blinking caret at the end of the assistant content, for a turn
   * that is still being written.
   */
  tailCaret?: boolean;
}

/** The optional reasons a user can give for a thumbs-down, in display order. */
const FEEDBACK_CATEGORY_CHOICES: ReadonlyArray<{
  value: FeedbackCategory;
  label: string;
}> = [
  { value: "wrong_answer", label: "The answer was wrong" },
  { value: "outdated_policy", label: "The policy information is out of date" },
  { value: "unhelpful_tone", label: "The answer was unhelpful" },
  { value: "missing_info", label: "The answer was missing information" },
  { value: "other", label: "Something else" },
];

/** Mirrors the server's cap so a note is cut off here rather than rejected there. */
const FEEDBACK_COMMENT_MAX_LENGTH = 1000;

/** How long the copy button keeps its "Copied" confirmation before reverting. */
const COPIED_RESET_MS = 2000;

/**
 * Whether any tool call on this message carries a claim the user has to act on,
 * or has just acted on.
 *
 * A pending confirmation is the only route from a prepared claim to a filed
 * one, so it has to be on screen without the user hunting for it. A receipt or
 * a cancellation note is the outcome of a decision already made, and it stays
 * visible for the same reason: it replaces the card in place, and collapsing
 * the disclosure at that moment would hide the only confirmation the user gets
 * that the decision landed.
 */
function carriesClaimDecision(message: Message): boolean {
  return (message.toolCalls ?? []).some((call) => {
    const result = call.result;
    return (
      asPendingClaimConfirmation(result) !== null ||
      asClaimConfirmationReceipt(result) !== null ||
      asClaimCancellationNote(result) !== null
    );
  });
}

/**
 * Read the session token the same way the auth context hydrates from storage.
 *
 * The transcript renders bubbles without threading a token prop down to them,
 * and the context hook cannot be used here because it requires a provider that
 * a bare render of this component does not have. Reading the same storage key
 * keeps the rating calls authenticated without changing the component's
 * contract.
 */
function storedSessionToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem("omnicare_token");
}

/** Markdown renderers. Fenced blocks get syntax highlighting; inline code does not. */
const markdownComponents: Components = {
  code({ className, children, ...props }) {
    // Only fenced blocks arrive with a language class, so its presence is what
    // distinguishes a block from inline code. The block renderer takes its own
    // ``className`` and ``style``, so the intrinsic props are not forwarded:
    // the spread would carry a plain CSSProperties ``style`` that the
    // highlighter's stylesheet map does not accept.
    const match = /language-(\w+)/.exec(className ?? "");
    if (match) {
      return (
        <SyntaxHighlighter
          style={oneLight}
          customStyle={{
            background: "rgb(var(--color-surface-secondary))",
            color: "rgb(var(--color-text))",
          }}
          language={match[1]}
          PreTag="div"
          className="rounded-lg !my-4"
        >
          {String(children).replace(/\n$/, "")}
        </SyntaxHighlighter>
      );
    }
    return (
      <code
        className="bg-insurance-surface-secondary px-1.5 py-0.5 rounded text-[13px] font-mono"
        {...props}
      >
        {children}
      </code>
    );
  },
};

export default function MessageBubble({
  message,
  onRetry,
  bare = false,
  tailCaret = false,
}: MessageBubbleProps) {
  const isUser = message.role === "user";
  const isError = message.role === "error";
  const [copied, setCopied] = useState(false);
  const [rating, setRating] = useState<FeedbackRating | null>(null);
  const [detailCategory, setDetailCategory] = useState<FeedbackCategory | "">(
    "",
  );
  const [detailComment, setDetailComment] = useState("");
  const [detailSent, setDetailSent] = useState(false);
  const [feedbackBusy, setFeedbackBusy] = useState(false);
  // Two clicks in the same event batch both read the same ``feedbackBusy``
  // state, which is still false for the second one, so the guard that actually
  // stops a double submission has to be a ref rather than state.
  const feedbackInFlight = useRef(false);
  // The copy confirmation is transient, so its timer is owned by a ref: the
  // effect at the bottom of this component cancels it when the bubble goes
  // away, rather than leaving a timeout to fire into an unmounted component.
  const copiedResetRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /**
   * The user's own choice for the tool-details disclosure, or ``null`` while
   * they have not made one.
   *
   * A claim decision opens the disclosure on its own: the confirmation card is
   * the only way a prepared claim becomes a filed one, and a collapsed row
   * labelled "Tool details" reads like debug output, which is how the card came
   * to sit one undiscovered click away from the journey that needs it.
   */
  const [toolDetailsChoice, setToolDetailsChoice] = useState<boolean | null>(
    null,
  );
  const claimDecisionVisible = carriesClaimDecision(message);
  const showToolDetails = claimDecisionVisible || toolDetailsChoice === true;

  const canRate = !isUser && !isError;
  // The feedback endpoint authorises on the id the backend persisted, so a
  // rating is sent against the backend id once the server has reported it and
  // falls back to the local id only before then -- or for a message the backend
  // never minted, such as a purely local error bubble. A just-streamed answer
  // adopts its backend id on the terminal frame; history already carries it as
  // ``id`` (and mirrors it onto ``backendId``).
  const feedbackMessageId = message.backendId ?? message.id;

  // The pending copy timer is cancelled with the component, so unmounting a
  // bubble mid-confirmation cannot leave a timeout to act on it.
  useEffect(
    () => () => {
      if (copiedResetRef.current !== null) {
        clearTimeout(copiedResetRef.current);
        copiedResetRef.current = null;
      }
    },
    [],
  );

  // A turn the stream opened but has put nothing in yet -- no words, no tool
  // trace, no citations, no failure. Without ``bare`` it gets an empty bubble
  // that collapses as soon as the first token lands, so the caller decides
  // whether that placeholder is worth its own shell.
  const isEmptyPlaceholderTurn =
    !isUser &&
    !isError &&
    message.content.trim() === "" &&
    (message.toolCalls ?? []).length === 0 &&
    (message.sources ?? []).length === 0 &&
    !message.error;
  const showBubbleShell = !(bare && isEmptyPlaceholderTurn);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(message.content);
    setCopied(true);
    // A second copy replaces the pending reset rather than stacking timers,
    // which would otherwise revert the confirmation on the first one's clock.
    if (copiedResetRef.current !== null) {
      clearTimeout(copiedResetRef.current);
    }
    copiedResetRef.current = setTimeout(() => {
      copiedResetRef.current = null;
      setCopied(false);
    }, COPIED_RESET_MS);
  };

  /**
   * Toggle the tool trace, unless a claim decision is on screen.
   *
   * A claim card is not debug output, so the disclosure never closes out from
   * under one: the user has to be able to read the claim and act on it without
   * first understanding what "Tool details" hides.
   */
  const toggleToolDetails = () => {
    if (claimDecisionVisible) return;
    setToolDetailsChoice((prev) => !prev);
  };

  /** Show a failure where the user is already looking instead of blocking them. */
  const reportFeedbackFailure = (failureText: string) => {
    toast.error(failureText, { id: `feedback-${message.id}` });
  };

  /**
   * Apply a rating optimistically, then reconcile with the server.
   *
   * Clicking the thumb that is already active withdraws the rating instead of
   * re-sending it, which is how the user changes their mind without a second
   * row. A request in flight blocks further clicks so a double tap cannot queue
   * two writes, and a failure restores exactly the state the click replaced.
   */
  const applyRating = async (next: FeedbackRating) => {
    if (feedbackInFlight.current) return;
    feedbackInFlight.current = true;
    setFeedbackBusy(true);

    const token = storedSessionToken();
    const previousRating = rating;

    try {
      if (previousRating === next) {
        setRating(null);
        await deleteFeedback(token, feedbackMessageId);
        return;
      }

      setRating(next);
      await submitFeedback(token, feedbackMessageId, next);
    } catch {
      setRating(previousRating);
      reportFeedbackFailure(
        previousRating === next
          ? "We could not remove your rating. Please try again."
          : "We could not save your rating. Please try again.",
      );
    } finally {
      feedbackInFlight.current = false;
      setFeedbackBusy(false);
    }
  };

  /**
   * Attach the optional reason and note to a thumbs-down.
   *
   * Both fields are optional, so an empty submit simply dismisses the form: the
   * rating itself is already stored and a second request would say nothing new.
   */
  const submitFeedbackDetail = async () => {
    if (feedbackInFlight.current) return;

    const comment = detailComment.trim();
    const category = detailCategory || undefined;
    if (!comment && !category) {
      setDetailSent(true);
      return;
    }

    const token = storedSessionToken();
    feedbackInFlight.current = true;
    setFeedbackBusy(true);
    try {
      await submitFeedback(
        token,
        feedbackMessageId,
        "down",
        comment || undefined,
        category,
      );
      setDetailSent(true);
    } catch {
      reportFeedbackFailure(
        "We could not save your feedback. Please try again.",
      );
    } finally {
      feedbackInFlight.current = false;
      setFeedbackBusy(false);
    }
  };

  const canRetry = !!onRetry && (isError || !!message.error);
  const timestamp = formatMessageTimestamp(message.timestamp);
  const showFeedbackDetail = canRate && rating === "down" && !detailSent;
  const showFeedbackThanks = canRate && rating !== null && !showFeedbackDetail;

  return (
    <div
      id={`message-${message.id}`}
      className={`flex w-full gap-3 ${
        isUser ? "justify-end" : "justify-start"
      } items-start message-enter`}
      data-testid={`message-${message.id}`}
      data-message-role={message.role}
    >
      {!isUser && (
        <div
          id={`avatar-${message.id}`}
          className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 ${
            isError
              ? "mt-3 bg-insurance-error/10"
              : "mt-3.5 bg-insurance-info/10"
          }`}
          data-testid={`avatar-${message.id}`}
        >
          {isError ? (
            <AlertCircle
              className="w-4 h-4 text-insurance-error"
              data-testid="error-icon"
            />
          ) : (
            <Bot
              className="w-4 h-4 text-insurance-info"
              data-testid="bot-icon"
            />
          )}
        </div>
      )}

      <div
        id={`message-content-${message.id}`}
        className={`flex flex-col ${
          isUser ? "items-end max-w-[78%]" : "items-start max-w-[92%]"
        }`}
        data-testid={`message-content-${message.id}`}
      >
        {showBubbleShell && (
          <div
            id={`message-bubble-${message.id}`}
            className={`text-[15px] leading-relaxed shadow-subtle group relative transition-colors duration-200
              ${
                isUser
                  ? "px-4 py-3 bg-insurance-info text-white rounded-2xl rounded-tr-sm"
                  : isError
                    ? "px-4 py-3 bg-insurance-error/10 border border-insurance-error/20 text-insurance-ink rounded-2xl rounded-bl-sm"
                    : "px-4 py-3.5 bg-insurance-surface border border-insurance-border text-insurance-ink rounded-2xl rounded-bl-sm hover:border-insurance-border-strong"
              }`}
            data-testid={`message-bubble-${message.id}`}
          >
            {isUser ? (
              <div
                id={`user-text-${message.id}`}
                className="whitespace-pre-wrap"
                data-testid={`user-text-${message.id}`}
              >
                {message.content}
              </div>
            ) : (
              <div
                id={`assistant-text-${message.id}`}
                className="prose prose-slate max-w-none prose-p:my-2 prose-pre:my-0 prose-pre:p-0 prose-code:text-insurance-ink prose-code:bg-insurance-surface-secondary prose-code:px-1 prose-code:py-0.5 prose-code:rounded"
                data-testid={`assistant-text-${message.id}`}
              >
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={markdownComponents}
                >
                  {message.content}
                </ReactMarkdown>
                {/* Still-writing marker, styled by the same pulse utilities the
                    typing dots use so a reduced-motion setting stops both. */}
                {tailCaret && (
                  <span
                    id={`tail-caret-${message.id}`}
                    aria-hidden="true"
                    className="ml-0.5 inline-block h-[1.05em] w-[2px] animate-pulse rounded-full bg-insurance-info align-middle"
                    data-testid="tail-caret"
                  />
                )}
              </div>
            )}

            {/* Failure note. Rendered inside the bubble so it reads as an
                annotation of the answer above it rather than a new turn. */}
            {message.error && (
              <div
                id={`error-note-${message.id}`}
                role="alert"
                className="mt-3 flex items-start gap-2 rounded-lg border border-insurance-error/20 bg-insurance-error/10 px-3 py-2 text-xs text-insurance-error"
                data-testid={`error-note-${message.id}`}
              >
                <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                <span>{message.error}</span>
              </div>
            )}

            {/* Hover actions, shared by one anchored wrapper so the copy and
                retry controls can never overlap. The opacity lives on the
                buttons, not the wrapper, so that :focus-visible can undo it: a
                parent is never the element the keyboard is focused on. The
                controls are permanent below ``sm`` and reveal on hover above
                it, because a touch screen has no hover to reveal them with. */}
            {!isUser && showBubbleShell && (
              <div className="absolute -top-3 right-2 flex items-center gap-1">
                {!isError && (
                  <button
                    type="button"
                    onClick={handleCopy}
                    className="p-1.5 bg-insurance-surface border border-insurance-border rounded-lg shadow-soft hover:shadow-medium transition-all duration-200 opacity-100 focus-visible:opacity-100 sm:opacity-0 sm:group-hover:sm:opacity-100 sm:focus-visible:sm:opacity-100"
                    aria-label={copied ? "Copied" : "Copy message"}
                    data-testid={`copy-button-${message.id}`}
                  >
                    {copied ? (
                      <Check className="w-3.5 h-3.5 text-insurance-success" />
                    ) : (
                      <Copy className="w-3.5 h-3.5 text-insurance-ink-secondary" />
                    )}
                  </button>
                )}
                {canRate && (
                  <>
                    <button
                      type="button"
                      onClick={() => applyRating("up")}
                      aria-label="Mark answer as helpful"
                      aria-pressed={rating === "up"}
                      disabled={feedbackBusy}
                      className={`p-1.5 bg-insurance-surface border border-insurance-border rounded-lg shadow-soft hover:shadow-medium transition-all duration-200 opacity-100 focus-visible:opacity-100 sm:opacity-0 sm:group-hover:sm:opacity-100 sm:focus-visible:sm:opacity-100 disabled:cursor-not-allowed disabled:opacity-50 ${
                        rating === "up"
                          ? "text-insurance-success"
                          : "text-insurance-ink-secondary"
                      }`}
                      data-testid={`feedback-up-${message.id}`}
                    >
                      <ThumbsUp className="w-3.5 h-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => applyRating("down")}
                      aria-label="Mark answer as unhelpful"
                      aria-pressed={rating === "down"}
                      disabled={feedbackBusy}
                      className={`p-1.5 bg-insurance-surface border border-insurance-border rounded-lg shadow-soft hover:shadow-medium transition-all duration-200 opacity-100 focus-visible:opacity-100 sm:opacity-0 sm:group-hover:sm:opacity-100 sm:focus-visible:sm:opacity-100 disabled:cursor-not-allowed disabled:opacity-50 ${
                        rating === "down"
                          ? "text-insurance-error"
                          : "text-insurance-ink-secondary"
                      }`}
                      data-testid={`feedback-down-${message.id}`}
                    >
                      <ThumbsDown className="w-3.5 h-3.5" />
                    </button>
                  </>
                )}
                {canRetry && (
                  <button
                    type="button"
                    onClick={onRetry}
                    className="p-1.5 bg-insurance-surface border border-insurance-border rounded-lg shadow-soft hover:shadow-medium transition-all duration-200 flex items-center gap-1 opacity-100 focus-visible:opacity-100 sm:opacity-0 sm:group-hover:sm:opacity-100 sm:focus-visible:sm:opacity-100"
                    aria-label="Retry"
                    data-testid={`retry-button-${message.id}`}
                  >
                    <RefreshCw className="w-3.5 h-3.5 text-insurance-ink-secondary" />
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        <div className="flex items-center gap-2 mt-1.5 px-1 opacity-70 group-hover:opacity-100 transition-opacity duration-200 sm:opacity-0 sm:group-hover:sm:opacity-100">
          <span
            className="text-[11px] text-insurance-ink-tertiary"
            data-testid={`message-time-${message.id}`}
          >
            {timestamp}
          </span>
          {isError && (
            <span className="text-[11px] text-insurance-error">
              Failed to send
            </span>
          )}
        </div>

        {/* Optional detail for a thumbs-down. Two fields and a submit, never a
            full report flow: the rating is already stored, and this only adds
            the reason behind it. */}
        {showFeedbackDetail && (
          <div
            id={`feedback-detail-${message.id}`}
            className="mt-2 w-full rounded-xl border border-insurance-border bg-insurance-surface-secondary/60 px-3 py-3 flex flex-col gap-2"
            data-testid={`feedback-detail-${message.id}`}
          >
            <label
              htmlFor={`feedback-category-${message.id}`}
              className="text-[11px] font-medium text-insurance-ink-secondary"
            >
              What went wrong? (optional)
            </label>
            <select
              id={`feedback-category-${message.id}`}
              value={detailCategory}
              onChange={(event) =>
                setDetailCategory(event.target.value as FeedbackCategory | "")
              }
              disabled={feedbackBusy}
              className="w-full rounded-lg border border-insurance-border bg-insurance-surface px-2.5 py-1.5 text-xs text-insurance-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-insurance-info/40 disabled:opacity-50"
              data-testid={`feedback-category-${message.id}`}
            >
              <option value="">Choose a reason</option>
              {FEEDBACK_CATEGORY_CHOICES.map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.label}
                </option>
              ))}
            </select>
            <label
              htmlFor={`feedback-comment-${message.id}`}
              className="text-[11px] font-medium text-insurance-ink-secondary"
            >
              Anything you would add? (optional)
            </label>
            <textarea
              id={`feedback-comment-${message.id}`}
              value={detailComment}
              onChange={(event) => setDetailComment(event.target.value)}
              maxLength={FEEDBACK_COMMENT_MAX_LENGTH}
              rows={2}
              disabled={feedbackBusy}
              placeholder="Tell us what was missing or wrong"
              className="w-full resize-none rounded-lg border border-insurance-border bg-insurance-surface px-2.5 py-1.5 text-xs text-insurance-ink placeholder:text-insurance-ink-tertiary focus:outline-none focus-visible:ring-2 focus-visible:ring-insurance-info/40 disabled:opacity-50"
              data-testid={`feedback-comment-${message.id}`}
            />
            <button
              type="button"
              onClick={submitFeedbackDetail}
              disabled={feedbackBusy}
              className="self-start inline-flex items-center gap-1.5 rounded-lg border border-insurance-border bg-insurance-surface px-2.5 py-1 text-[11px] font-medium text-insurance-ink-secondary hover:text-insurance-ink hover:border-insurance-border-strong transition-colors disabled:cursor-not-allowed disabled:opacity-50"
              data-testid={`feedback-submit-${message.id}`}
            >
              {feedbackBusy ? "Sending" : "Send details"}
            </button>
          </div>
        )}

        {/* Inline confirmation, so the user sees the rating landed and can
            change their mind by choosing the other thumb. */}
        {showFeedbackThanks && (
          <p
            id={`feedback-thanks-${message.id}`}
            className="mt-1.5 px-1 text-[11px] text-insurance-ink-tertiary"
            data-testid={`feedback-thanks-${message.id}`}
          >
            Thanks for the feedback
          </p>
        )}

        {!isUser && message.sources && message.sources.length > 0 && (
          <div
            id={`sources-container-${message.id}`}
            className="mt-2 w-full"
            data-testid={`sources-container-${message.id}`}
          >
            <SourcesBadge sources={message.sources} messageId={message.id} />
          </div>
        )}

        {!isUser && message.toolCalls && message.toolCalls.length > 0 && (
          <div className="mt-2 w-full">
            <button
              type="button"
              onClick={toggleToolDetails}
              className="flex items-center gap-1 text-xs text-insurance-ink-secondary hover:text-insurance-ink transition-colors"
              aria-expanded={showToolDetails}
              data-testid={`tool-details-toggle-${message.id}`}
            >
              {showToolDetails ? (
                <ChevronDown className="w-3.5 h-3.5" />
              ) : (
                <ChevronRight className="w-3.5 h-3.5" />
              )}
              <span>Tool details</span>
            </button>
            {showToolDetails && (
              <div
                id={`tool-calls-container-${message.id}`}
                className="mt-2 w-full flex flex-col gap-2"
                data-testid={`tool-calls-container-${message.id}`}
              >
                <ToolCallBadge toolCalls={message.toolCalls} />
              </div>
            )}
          </div>
        )}
      </div>

      {isUser && (
        <div
          id={`user-avatar-${message.id}`}
          className="w-8 h-8 rounded-full bg-insurance-info text-white flex items-center justify-center flex-shrink-0 mt-3 shadow-subtle"
          data-testid={`user-avatar-${message.id}`}
        >
          <User className="w-4 h-4" data-testid="user-icon" />
        </div>
      )}
    </div>
  );
}
