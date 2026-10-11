/**
 * ToolCallBadge component.
 *
 * Renders a transparent view of the AI agent's tool invocations during a
 * single assistant message. For corpus tools (e.g. ``query_policy``) it shows
 * an expandable details element with arguments and raw JSON result. For
 * ``get_claim_status`` it renders a purpose-built status card, and for
 * ``prepare_claim_submission`` the confirmation card that turns a staged claim
 * into a filed one, because both have a known shape and raw JSON would only
 * obscure them.
 *
 * Design rationale:
 *   - Tool transparency builds user trust by showing exactly what the agent
 *     did to arrive at its answer.
 *   - A rich card for the claim lookup reduces cognitive load compared to raw
 *     JSON, and lets the card show only the fields the backend returns.
 *   - The details/summary pattern keeps the default view compact.
 *   - A prepared claim is a decision the user has to make, so its card is
 *     rendered wherever the claim's tool call appears rather than being folded
 *     into the generic trace.
 */

import {
  GET_CLAIM_STATUS_TOOL,
  PREPARE_CLAIM_SUBMISSION_TOOL,
  asClaimCancellationNote,
  asClaimConfirmationReceipt,
  asPendingClaimConfirmation,
} from "@/lib/api";
import {
  ClaimCancellationNotice,
  ClaimConfirmCard,
  ClaimReceipt,
  claimConfirmationTarget,
} from "./ClaimConfirmCard";
import { ClaimStatusResult, ToolCall } from "@/types/chat";
import { ChevronDown, AlertCircle } from "lucide-react";

interface ToolCallBadgeProps {
  /** Array of tool calls made during the generation of an assistant message. */
  toolCalls: ToolCall[];
}

/** Claim statuses the backend can produce, with the colour each one carries. */
const STATUS_COLORS: Record<string, string> = {
  Submitted: "bg-insurance-info",
  "Under Review": "bg-insurance-warning",
  Approved: "bg-insurance-success",
  Paid: "bg-insurance-success",
  Denied: "bg-insurance-error",
  Closed: "bg-insurance-ink-tertiary",
};

/** Colour for a status that is not in the map, so an unknown value never renders blank. */
const FALLBACK_STATUS_COLOR = "bg-insurance-ink-tertiary";

/**
 * Amounts cross the wire as either a JSON number or a decimal string, and a
 * failed lookup omits the field entirely.
 */
function formatAmount(amount: ClaimStatusResult["amount"]): string {
  const numeric = typeof amount === "string" ? Number(amount) : amount;
  if (typeof numeric !== "number" || !Number.isFinite(numeric)) return "—";
  return `$${numeric.toLocaleString()}`;
}

function isClaimStatusResult(value: unknown): value is ClaimStatusResult {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export default function ToolCallBadge({ toolCalls }: ToolCallBadgeProps) {
  if (!toolCalls || toolCalls.length === 0) return null;

  return (
    <div className="flex flex-col gap-2 mt-4">
      {toolCalls.map((call, idx) => (
        <ToolCallItem key={idx} call={call} />
      ))}
    </div>
  );
}

function StatusCard({ result }: { result: ClaimStatusResult }) {
  if (result?.error) {
    return (
      <div className="bg-insurance-error/10 border border-insurance-error/20 rounded-xl p-4 mt-2">
        <div className="flex items-center space-x-2 text-insurance-error mb-2">
          <AlertCircle className="w-5 h-5" />
          <span className="font-medium">Claim Not Found</span>
        </div>
        <p className="text-sm text-insurance-ink-secondary">{result.error}</p>
      </div>
    );
  }

  const color = STATUS_COLORS[result.status ?? ""] ?? FALLBACK_STATUS_COLOR;

  return (
    <div className="bg-insurance-surface border border-insurance-border rounded-xl p-5 mt-2 shadow-subtle">
      <div className="flex justify-between items-start mb-4">
        <div>
          <p className="text-xs text-insurance-ink-tertiary uppercase tracking-wider mb-1">
            Claim Reference
          </p>
          <h3 className="text-lg font-mono text-insurance-ink">
            {result.claim_id}
          </h3>
        </div>
        <div
          className={`flex items-center space-x-1.5 px-3 py-1 rounded-full bg-insurance-surface-secondary border border-insurance-border`}
          data-testid="claim-status-pill"
        >
          <div className={`w-2 h-2 rounded-full ${color} animate-pulse`}></div>
          <span className="text-sm font-medium text-insurance-ink">
            {result.status ?? "Unknown"}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 text-sm">
        <div>
          <p className="text-insurance-ink-tertiary">Type</p>
          <p className="text-insurance-ink font-medium">
            {result.claim_type ?? "—"}
          </p>
        </div>
        <div>
          <p className="text-insurance-ink-tertiary">Amount</p>
          <p className="text-insurance-ink font-medium">
            {formatAmount(result.amount)}
          </p>
        </div>
        <div className="col-span-2">
          <p className="text-insurance-ink-tertiary">Policy Number</p>
          <p className="text-insurance-ink font-medium">
            {result.policy_number ?? "—"}
          </p>
        </div>
        {result.citation && (
          <p className="col-span-2 text-xs text-insurance-ink-tertiary border-t border-insurance-border pt-3">
            {result.citation}
          </p>
        )}
      </div>
    </div>
  );
}

/**
 * The confirmation card, the receipt, or the cancellation note for one prepared
 * claim.
 *
 * A prepare that failed validation never produced a token, so it is left in the
 * generic trace where its error is still visible.
 */
function PreparedClaimCall({ call }: { call: ToolCall }) {
  const receipt = asClaimConfirmationReceipt(call.result);
  if (receipt) return <ClaimReceipt receipt={receipt} />;

  const note = asClaimCancellationNote(call.result);
  if (note) return <ClaimCancellationNotice note={note} />;

  const pending = asPendingClaimConfirmation(call.result);
  if (pending) {
    return (
      <ClaimConfirmCard
        claimData={pending.claimData}
        token={pending.token}
        expiresAt={pending.expiresAt}
        onConfirmed={(outcome) =>
          claimConfirmationTarget(pending.token)?.recordConfirmation(outcome)
        }
        onCancelled={(outcome) =>
          claimConfirmationTarget(pending.token)?.recordCancellation(outcome)
        }
      />
    );
  }

  return <GenericToolTrace call={call} />;
}

function ToolCallItem({ call }: { call: ToolCall }) {
  if (call.name === GET_CLAIM_STATUS_TOOL && isClaimStatusResult(call.result)) {
    return <StatusCard result={call.result} />;
  }
  if (call.name === PREPARE_CLAIM_SUBMISSION_TOOL) {
    return <PreparedClaimCall call={call} />;
  }
  return <GenericToolTrace call={call} />;
}

/** The raw arguments and result of a tool, for anything without a purpose card. */
function GenericToolTrace({ call }: { call: ToolCall }) {
  const color =
    call.name === "query_policy"
      ? "bg-insurance-info/10 border-insurance-info/20 text-insurance-info"
      : "bg-insurance-surface border-insurance-border text-insurance-ink";

  return (
    <details className="group flex flex-col items-start gap-1 w-full">
      <summary
        className={`flex w-fit items-center gap-2 px-3 py-1.5 text-xs font-medium border rounded-full transition-colors hover:brightness-95 cursor-pointer list-none ${color}`}
      >
        <span className="font-mono">{call.name}</span>
        <ChevronDown className="w-3 h-3 ml-1 transition-transform group-open:rotate-180" />
      </summary>

      <div className="w-full mt-1 p-3 bg-insurance-surface border border-insurance-border rounded-lg text-xs font-mono text-insurance-ink overflow-x-auto">
        <div className="mb-2">
          <span className="text-insurance-ink-tertiary select-none">
            Arguments:
          </span>
          <pre className="mt-1 text-insurance-ink">
            {JSON.stringify(call.arguments, null, 2) ?? ""}
          </pre>
        </div>
        {call.result !== undefined && call.result !== null ? (
          <div>
            <span className="text-insurance-ink-tertiary select-none">
              Result:
            </span>
            <pre className="mt-1 text-insurance-success">
              {JSON.stringify(call.result, null, 2) ?? ""}
            </pre>
          </div>
        ) : null}
      </div>
    </details>
  );
}
