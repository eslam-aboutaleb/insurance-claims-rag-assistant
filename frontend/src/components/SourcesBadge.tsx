/**
 * SourcesBadge component.
 *
 * Renders the policy source citations returned by the AI agent. When the agent
 * answers a policy coverage question it includes references to the specific
 * policy sections used to construct the answer. This component surfaces those
 * references so users can verify the grounding of the response.
 *
 * Interaction:
 *   - The header toggles the whole list, which starts collapsed.
 *   - A pill that carries a retrieved excerpt expands to reveal it, so the
 *     citation never overstates what it can show.
 *   - A pill without an excerpt is plain text with a ``title`` attribute and
 *     no interactive affordance, because there is nothing to open.
 */

import { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { SourceCitation, describeCitation } from "@/types/chat";

interface SourcesBadgeProps {
  /** Citations from the agent response, in arrival order. */
  sources: SourceCitation[];
  /**
   * The message these citations belong to.
   *
   * Every assistant answer with citations renders one of these badges, so the
   * DOM ids below have to carry the message id: identical ids would leave
   * ``aria-controls`` pointing at whichever list comes first in the document
   * instead of the one the button actually opens.
   */
  messageId: string;
}

export default function SourcesBadge({
  sources,
  messageId,
}: SourcesBadgeProps) {
  // Collapsed until the user asks. A multi-citation answer otherwise opens
  // every transcript turn looking like a wall of pills before anyone has asked
  // what the answer is grounded in, and the count in the header stays visible
  // either way.
  const [expanded, setExpanded] = useState(false);

  if (!sources || sources.length === 0) return null;

  const toggleId = `sources-toggle-${messageId}`;
  const listId = `sources-list-${messageId}`;

  return (
    <div
      id={`sources-badge-${messageId}`}
      className="flex flex-col items-start gap-2"
      data-testid="sources-badge"
    >
      <button
        id={toggleId}
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-insurance-ink-secondary bg-insurance-surface border border-insurance-border hover:bg-insurance-surface-secondary rounded-full transition-colors duration-200"
        data-testid="sources-toggle"
        aria-expanded={expanded}
        aria-controls={listId}
      >
        <span data-testid="sources-count">Sources ({sources.length})</span>
        {expanded ? (
          <ChevronUp className="w-3 h-3" data-testid="sources-chevron-up" />
        ) : (
          <ChevronDown className="w-3 h-3" data-testid="sources-chevron-down" />
        )}
      </button>

      {expanded && (
        <div
          id={listId}
          className="flex flex-col gap-2 mt-1 animate-fade-in"
          data-testid="sources-list"
        >
          {sources.map((source, idx) => (
            <SourcePill
              key={idx}
              source={source}
              index={idx}
              messageId={messageId}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * One citation, either inert or expandable depending on whether the backend
 * supplied an excerpt.
 */
function SourcePill({
  source,
  index,
  messageId,
}: {
  source: SourceCitation;
  index: number;
  messageId: string;
}) {
  const [open, setOpen] = useState(false);
  const citation = describeCitation(source);
  const pillId = `source-${messageId}-${index}`;
  const excerptId = `source-excerpt-${messageId}-${index}`;

  if (!citation.excerpt) {
    return (
      <span
        id={pillId}
        className="px-3 py-1.5 bg-insurance-surface border border-insurance-border text-insurance-ink text-xs rounded-lg cursor-default"
        title={citation.title}
        data-testid={`source-${index}`}
      >
        {citation.label}
      </span>
    );
  }

  return (
    <div
      className="flex flex-col items-start"
      data-testid={`source-wrapper-${index}`}
    >
      <button
        id={pillId}
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="px-3 py-1.5 bg-insurance-surface border border-insurance-border text-insurance-ink text-xs rounded-lg text-left hover:border-insurance-info/40 transition-colors"
        title={citation.title}
        data-testid={`source-${index}`}
        aria-expanded={open}
        aria-controls={excerptId}
      >
        {citation.label}
      </button>
      {open && (
        <p
          id={excerptId}
          className="mt-1 ml-3 pl-3 border-l border-insurance-border text-xs text-insurance-ink-secondary leading-relaxed"
          data-testid={`source-excerpt-${index}`}
        >
          {citation.excerpt}
        </p>
      )}
    </div>
  );
}
