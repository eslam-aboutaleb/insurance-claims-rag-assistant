/**
 * ClaimConfirmCard component.
 *
 * Renders a claim the agent has prepared but not yet filed, so the user can
 * review the four values under review and confirm or cancel them. Nothing is
 * written until Confirm succeeds: the backend's two-step prepare/confirm flow
 * is the only path from a staged claim to a filed one, and this card is the UI
 * half of the second step.
 *
 * Design rationale:
 *   - The card owns the confirm/cancel calls, because the decisions it has to
 *     survive (a dead token, an unreachable server, an in-flight double click)
 *     are all answers to a request only it can see.
 *   - ``onConfirmed`` and ``onCancelled`` are notifications, not delegation:
 *     the card still resolves without an owner, so it can never depend on a
 *     hook that a test did not render.
 *   - Review notes never block Confirm. A claim above a policy cap may still be
 *     worth filing, so the user is told and left to decide.
 */

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  ExternalLink,
  Loader2,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import {
  ApiError,
  ClaimCancelUnavailableError,
  ClaimCancellationNote,
  ClaimConfirmationReceipt,
  ClaimDetailView,
  ClaimFailure,
  cancelClaimSubmission,
  confirmClaim,
  lookupConfirmedClaim,
  PreparedClaimData,
} from "@/lib/api";

/**
 * A confirmation card is rendered by ``ToolCallBadge``, which only ever receives
 * the tool call, so the chat hook that owns the message list cannot be reached
 * through props. The hook publishes a recorder per confirmation token instead
 * and the card resolves it by its own token: a token exists for exactly one
 * pending submission, so two chat views can never claim each other's outcome.
 */
export interface ClaimConfirmationRecorder {
  /** The confirmation succeeded; store the receipt on the turn. */
  recordConfirmation: (receipt: ClaimConfirmationReceipt) => void;
  /** The preparation was cancelled, or left to expire. */
  recordCancellation: (note: ClaimCancellationNote) => void;
}

/** Recorders keyed by confirmation token, so a card can find its owner. */
const recordersByToken = new Map<string, ClaimConfirmationRecorder>();

/**
 * Publish where a prepared claim's outcome belongs.
 *
 * @param token - Confirmation token of the prepared claim.
 * @param recorder - The owning chat hook's recorder for that claim.
 * @returns A function that withdraws the recorder, for the owner's cleanup.
 */
export function registerClaimConfirmationRecorder(
  token: string,
  recorder: ClaimConfirmationRecorder,
): () => void {
  recordersByToken.set(token, recorder);
  return () => {
    if (recordersByToken.get(token) === recorder) {
      recordersByToken.delete(token);
    }
  };
}

/**
 * Look up the recorder for a prepared claim.
 *
 * @param token - Confirmation token of the prepared claim.
 * @returns The owner's recorder, or ``undefined`` when no chat hook owns it.
 */
export function claimConfirmationTarget(
  token: string,
): ClaimConfirmationRecorder | undefined {
  return recordersByToken.get(token);
}

/** Water damage cover from Section 1 of the policy, used only to warn. */
const WATER_DAMAGE_COVER_CAP = 25000;

/** The claim form refuses a description shorter than this. */
const MINIMUM_DESCRIPTION_LENGTH = 10;

/**
 * Format a claimed amount as whole dollars.
 *
 * Cents survive when the agent sent them, but a value that is already whole
 * never renders as "$3,500.00" on a review card.
 */
function formatClaimAmount(amount: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(amount);
}

/**
 * Format a date the way the transcript does.
 *
 * The locale is pinned so a receipt reads identically in every browser and
 * every test run, rather than shifting with the visitor's system locale.
 */
function formatClaimDate(date: Date): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
  }).format(date);
}

/** Time left on a confirmation token, as a short human countdown. */
function formatCountdown(remainingMs: number): string {
  // A token with no readable expiry is not a shrinking one, so it is described
  // as valid rather than as a countdown that would never reach zero.
  if (!Number.isFinite(remainingMs)) return "Valid";
  if (remainingMs <= 0) return "Expired";
  const totalSeconds = Math.floor(remainingMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes === 0) return `${seconds}s left`;
  return `${minutes}m ${seconds.toString().padStart(2, "0")}s left`;
}

/**
 * Values worth flagging before the user commits, without blocking them.
 *
 * These are the checks a policyholder can read in one glance: the cap the
 * policy states for this kind of loss, and the field the claim form itself
 * would reject. Anything else is the backend's to refuse.
 */
function reviewNotes(claimData: PreparedClaimData): string[] {
  const notes: string[] = [];
  const isWaterDamage = /water/i.test(claimData.claim_type);
  if (isWaterDamage && claimData.amount > WATER_DAMAGE_COVER_CAP) {
    notes.push(
      `Water damage is covered up to ${formatClaimAmount(
        WATER_DAMAGE_COVER_CAP,
      )} under your policy, so the amount above that limit may not be reimbursed. You can still file the claim; an adjuster will review the amount.`,
    );
  }
  if (claimData.description.trim().length < MINIMUM_DESCRIPTION_LENGTH) {
    notes.push(
      "The description is shorter than the claim form requires, so this submission may be rejected. Confirm anyway, or ask the assistant for a fuller description.",
    );
  }
  return notes;
}

/**
 * Classify a failed confirmation by what the user can do about it.
 *
 * The backend reports a spent token as one 400 whether it was never issued,
 * timed out, or already confirmed, so the only signal available is its wording:
 * the no-row message names a token that is gone, and the expiry message names
 * the clock. A confirmation made in another window or on another device leaves
 * exactly the first of those behind here.
 */
function describeFailure(error: unknown): ClaimFailure {
  const fallback = "Something went wrong. Please try again.";
  if (!(error instanceof ApiError)) {
    return { kind: "retryable", message: fallback };
  }
  const message = error.message || fallback;
  if (error.status === 0) {
    return {
      kind: "retryable",
      message:
        "We could not reach the server, so your claim has not been filed. Try again.",
    };
  }
  if (error.status === 400) {
    return /invalid|already used/i.test(message)
      ? { kind: "unusable", message }
      : { kind: "expired", message };
  }
  return { kind: "retryable", message };
}

/** Recovery guidance for a token that is gone. */
function recoveryGuidance(kind: ClaimFailure["kind"]): string {
  if (kind === "expired") {
    return "This confirmation is no longer valid. Ask the assistant to prepare your claim again to get a new one.";
  }
  if (kind === "unusable") {
    return "If you already confirmed this claim in another window or on another device, it has been filed — ask the assistant for its status. Otherwise, ask it to prepare the claim again.";
  }
  return "Your claim is still prepared and the token is still valid. Try again.";
}

/** What the card is doing right now. */
type CardPhase = "pending" | "confirming" | "cancelling" | "settled";

export interface ClaimConfirmCardProps {
  /** The claim values the user is being asked to review. */
  claimData: PreparedClaimData;
  /** Single-use confirmation token for this prepared claim. */
  token: string;
  /** ISO timestamp after which the token is refused. */
  expiresAt: string;
  /** The claim was filed. The owner records it; the card shows it meanwhile. */
  onConfirmed: (receipt: ClaimConfirmationReceipt) => void;
  /** The user cancelled. The owner records it; the card shows it meanwhile. */
  onCancelled: (note: ClaimCancellationNote) => void;
}

export function ClaimConfirmCard({
  claimData,
  token,
  expiresAt,
  onConfirmed,
  onCancelled,
}: ClaimConfirmCardProps) {
  const { token: authToken } = useAuth();
  const [now, setNow] = useState(() => Date.now());
  const [phase, setPhase] = useState<CardPhase>("pending");
  const [failure, setFailure] = useState<ClaimFailure | null>(null);
  const [receipt, setReceipt] = useState<ClaimConfirmationReceipt | null>(null);
  const [cancellation, setCancellation] =
    useState<ClaimCancellationNote | null>(null);
  const [notes] = useState(() => reviewNotes(claimData));

  const expiresAtMs = Date.parse(expiresAt);
  const hasExpiry = Number.isFinite(expiresAtMs);
  const remainingMs = hasExpiry ? expiresAtMs - now : Number.POSITIVE_INFINITY;
  const isExpired = remainingMs <= 0;
  const isBusy = phase === "confirming" || phase === "cancelling";

  // The countdown only needs to run while the token is still alive, and it is
  // the only clock the card has: an expired token cannot be un-expired, so the
  // interval stops the moment the deadline passes.
  useEffect(() => {
    if (!hasExpiry || isExpired) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [hasExpiry, isExpired]);

  const handleConfirm = useCallback(async () => {
    // An expired token is refused by the backend anyway, and a second click
    // while one is in flight would be a double submission the row lock has to
    // absorb rather than something the UI should invite.
    if (isExpired || isBusy) return;

    setPhase("confirming");
    setFailure(null);
    try {
      const response = await confirmClaim(authToken, token);
      if (!response.success) {
        setPhase("pending");
        setFailure({
          kind: "retryable",
          message: response.message || "The claim could not be submitted.",
        });
        return;
      }
      const confirmed: ClaimConfirmationReceipt = {
        kind: "claim_confirmation_receipt",
        claimId: response.claim_id ?? "",
        status: response.status ?? "Submitted",
        message: response.message,
      };
      setReceipt(confirmed);
      setPhase("settled");
      onConfirmed(confirmed);
    } catch (error) {
      // The token is untouched by a refusal or a transport failure, so the card
      // stays exactly as interactive as it was.
      setPhase("pending");
      setFailure(describeFailure(error));
    }
  }, [authToken, isBusy, isExpired, onConfirmed, token]);

  const handleCancel = useCallback(async () => {
    if (isBusy) return;

    setPhase("cancelling");
    setFailure(null);
    try {
      const result = await cancelClaimSubmission(authToken, token);
      const note: ClaimCancellationNote = {
        kind: "claim_cancellation_note",
        cancelled: result.cancelled,
        message: result.message,
      };
      setCancellation(note);
      setPhase("settled");
      onCancelled(note);
    } catch (error) {
      if (error instanceof ClaimCancelUnavailableError) {
        // Nothing was cancelled and nothing is claimed to be. The submission is
        // still live, so the honest outcome is that it will expire.
        const note: ClaimCancellationNote = {
          kind: "claim_cancellation_note",
          cancelled: false,
          message: error.message,
        };
        setCancellation(note);
        setPhase("settled");
        onCancelled(note);
        return;
      }
      setPhase("pending");
      setFailure(describeFailure(error));
    }
  }, [authToken, isBusy, onCancelled, token]);

  // A settled card is the fallback for one no chat hook owns. When a hook does
  // own it, the recorded outcome replaces this card on the same commit.
  if (phase === "settled" && receipt) {
    return <ClaimReceipt receipt={receipt} />;
  }
  if (phase === "settled" && cancellation) {
    return <ClaimCancellationNotice note={cancellation} />;
  }

  return (
    <div
      className="bg-insurance-surface border border-insurance-border rounded-xl p-5 mt-2 shadow-subtle"
      data-testid="claim-confirm-card"
    >
      <div className="flex items-start justify-between gap-3 mb-4">
        <div>
          <p className="text-xs text-insurance-ink-tertiary uppercase tracking-wider mb-1">
            Confirm your claim
          </p>
          <h3 className="text-lg font-semibold text-insurance-ink">
            Review and submit
          </h3>
        </div>
        <span
          className={`flex items-center gap-1.5 px-3 py-1 rounded-full border text-xs font-medium ${
            isExpired
              ? "bg-insurance-error/10 border-insurance-error/20 text-insurance-error"
              : "bg-insurance-surface-secondary border-insurance-border text-insurance-ink-secondary"
          }`}
          data-testid="claim-confirm-countdown"
        >
          <Clock className="w-3.5 h-3.5" />
          {isExpired ? "Expired" : formatCountdown(remainingMs)}
        </span>
      </div>

      <dl className="grid grid-cols-2 gap-4 text-sm">
        <div>
          <dt className="text-insurance-ink-tertiary">Policy Number</dt>
          <dd className="text-insurance-ink font-medium">
            {claimData.policy_number || "—"}
          </dd>
        </div>
        <div>
          <dt className="text-insurance-ink-tertiary">Claim Type</dt>
          <dd className="text-insurance-ink font-medium">
            {claimData.claim_type || "—"}
          </dd>
        </div>
        <div>
          <dt className="text-insurance-ink-tertiary">Amount</dt>
          <dd className="text-insurance-ink font-medium">
            {formatClaimAmount(claimData.amount)}
          </dd>
        </div>
        <div className="col-span-2">
          <dt className="text-insurance-ink-tertiary">Description</dt>
          <dd className="text-insurance-ink font-medium whitespace-pre-wrap">
            {claimData.description || "—"}
          </dd>
        </div>
      </dl>

      {notes.map((note) => (
        <p
          key={note}
          className="mt-4 flex items-start gap-2 rounded-lg border border-insurance-warning/25 bg-insurance-warning/10 px-3 py-2 text-xs text-insurance-ink-secondary"
          data-testid="claim-confirm-note"
        >
          <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5 text-insurance-warning" />
          <span>{note}</span>
        </p>
      ))}

      {failure && (
        <div
          role="alert"
          className="mt-4 rounded-lg border border-insurance-error/20 bg-insurance-error/10 px-3 py-2"
          data-testid="claim-confirm-error"
        >
          <div className="flex items-start gap-2 text-xs text-insurance-error">
            <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
            <span>{failure.message}</span>
          </div>
          <p className="mt-1.5 text-xs text-insurance-ink-secondary">
            {recoveryGuidance(failure.kind)}
          </p>
        </div>
      )}

      {isExpired && (
        <p
          className="mt-4 text-xs text-insurance-ink-tertiary"
          data-testid="claim-confirm-expired-note"
        >
          This confirmation expired
          {hasExpiry ? ` on ${formatClaimDate(new Date(expiresAtMs))}` : ""}.
          Ask the assistant to prepare your claim again to get a new
          confirmation.
        </p>
      )}

      <div className="flex items-center gap-3 mt-5">
        <button
          type="button"
          onClick={handleConfirm}
          disabled={isExpired || isBusy}
          className="inline-flex items-center gap-2 px-4 py-2 bg-insurance-info text-white text-sm font-medium rounded-lg hover:bg-insurance-info/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          data-testid="confirm-claim-button"
        >
          {phase === "confirming" ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              Confirming
            </>
          ) : (
            "Confirm claim"
          )}
        </button>
        <button
          type="button"
          onClick={handleCancel}
          disabled={isBusy}
          className="px-4 py-2 bg-insurance-surface border border-insurance-border text-insurance-ink-secondary text-sm font-medium rounded-lg hover:text-insurance-ink hover:border-insurance-border-strong transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          data-testid="cancel-claim-button"
        >
          {phase === "cancelling" ? "Cancelling" : "Cancel"}
        </button>
        {hasExpiry && !isExpired && (
          <span className="text-xs text-insurance-ink-tertiary">
            Until {formatClaimDate(new Date(expiresAtMs))}
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * Receipt for a filed claim.
 *
 * The claim is opened by reading it back from the claims API rather than by
 * linking to a URL: the endpoint needs a bearer token, so an anchor would only
 * produce a 401. A failed read never invalidates the receipt — the claim exists
 * whether or not its detail can be shown again.
 */
export function ClaimReceipt({
  receipt,
}: {
  receipt: ClaimConfirmationReceipt;
}) {
  const { token: authToken } = useAuth();
  const [claim, setClaim] = useState<ClaimDetailView | null>(null);
  const [lookupState, setLookupState] = useState<"idle" | "loading" | "failed">(
    "idle",
  );

  const openClaim = useCallback(async () => {
    if (claim || lookupState === "loading" || !receipt.claimId) return;
    setLookupState("loading");
    try {
      setClaim(await lookupConfirmedClaim(authToken, receipt.claimId));
      setLookupState("idle");
    } catch {
      setLookupState("failed");
    }
  }, [authToken, claim, lookupState, receipt.claimId]);

  return (
    <div
      className="bg-insurance-success/10 border border-insurance-success/20 rounded-xl p-5 mt-2"
      data-testid="claim-receipt"
    >
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex items-start gap-3">
          <CheckCircle2 className="w-5 h-5 flex-shrink-0 mt-1 text-insurance-success" />
          <div>
            <p className="text-xs text-insurance-success uppercase tracking-wider mb-1">
              Claim submitted
            </p>
            <h3 className="text-lg font-mono text-insurance-ink">
              {receipt.claimId}
            </h3>
          </div>
        </div>
        <span className="flex items-center gap-1.5 px-3 py-1 rounded-full bg-insurance-surface border border-insurance-border">
          <div className="w-2 h-2 rounded-full bg-insurance-success" />
          <span className="text-sm font-medium text-insurance-ink">
            {receipt.status || "Submitted"}
          </span>
        </span>
      </div>

      <p className="text-sm text-insurance-ink-secondary">{receipt.message}</p>

      {claim ? (
        <dl className="grid grid-cols-2 gap-4 text-sm mt-4 border-t border-insurance-success/20 pt-3">
          <div>
            <dt className="text-insurance-ink-tertiary">Policy Number</dt>
            <dd className="text-insurance-ink font-medium">
              {claim.policy_number ?? "—"}
            </dd>
          </div>
          <div>
            <dt className="text-insurance-ink-tertiary">Claim Type</dt>
            <dd className="text-insurance-ink font-medium">
              {claim.claim_type ?? "—"}
            </dd>
          </div>
          <div>
            <dt className="text-insurance-ink-tertiary">Status</dt>
            <dd className="text-insurance-ink font-medium">
              {claim.status ?? "—"}
            </dd>
          </div>
          {claim.description && (
            <div className="col-span-2">
              <dt className="text-insurance-ink-tertiary">Description</dt>
              <dd className="text-insurance-ink font-medium whitespace-pre-wrap">
                {claim.description}
              </dd>
            </div>
          )}
        </dl>
      ) : (
        <button
          type="button"
          onClick={openClaim}
          disabled={lookupState === "loading"}
          className="mt-3 inline-flex items-center gap-1.5 text-sm font-medium text-insurance-success hover:text-insurance-ink transition-colors disabled:opacity-50"
          data-testid="claim-receipt-link"
        >
          {lookupState === "loading" ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <ExternalLink className="w-4 h-4" />
          )}
          View claim {receipt.claimId}
        </button>
      )}

      {lookupState === "failed" && (
        <p className="mt-2 text-xs text-insurance-ink-tertiary">
          The claim&rsquo;s details could not be loaded right now. The claim was
          still filed — ask the assistant for its status.
        </p>
      )}
    </div>
  );
}

/** Outcome of a cancellation the backend could not carry out. */
export function ClaimCancellationNotice({
  note,
}: {
  note: ClaimCancellationNote;
}) {
  return (
    <div
      className={`border rounded-xl p-4 mt-2 ${
        note.cancelled
          ? "bg-insurance-surface border-insurance-border"
          : "bg-insurance-warning/10 border-insurance-warning/20"
      }`}
      data-testid="claim-cancellation-note"
    >
      <div className="flex items-start gap-2">
        {note.cancelled ? (
          <XCircle className="w-5 h-5 flex-shrink-0 mt-0.5 text-insurance-ink-tertiary" />
        ) : (
          <ShieldCheck className="w-5 h-5 flex-shrink-0 mt-0.5 text-insurance-warning" />
        )}
        <div>
          <p className="text-sm font-medium text-insurance-ink">
            {note.cancelled ? "Preparation cancelled" : "No claim was filed"}
          </p>
          <p className="text-xs text-insurance-ink-secondary mt-1">
            {note.message}
          </p>
        </div>
      </div>
    </div>
  );
}

export default ClaimConfirmCard;
