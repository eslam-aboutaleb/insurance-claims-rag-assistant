/**
 * Sidebar component.
 *
 * Provides the persistent navigation rail on the left side of the chat
 * interface. Contains the app branding, a "New Chat" action, a title filter,
 * the user's conversation history list, and a logout button.
 *
 * Data flow:
 *   - Conversation history is fetched via React Query using the
 *     ``getConversations`` API call, keyed on ``refreshTrigger`` and the
 *     active filter, so the parent can force a refetch after creating a new
 *     conversation and every filter keystroke re-queries with ``?q=``.
 *   - Selecting a conversation calls ``onSelectChat`` with the conversation
 *     ID; the parent then switches the ChatWindow into read-only history mode.
 *   - Renaming and deleting a conversation call the API, then invalidate the
 *     ``["conversations"]`` queries so every list re-reads the server. A
 *     delete also reports the id through ``onDeleteConversation`` so the
 *     parent can leave a transcript the user has just deleted.
 *   - The sidebar is hidden on mobile by default and toggled via the
 *     ``sidebarOpen`` state in the parent page component.
 *
 * The row actions (rename, delete) are mounted only while their row is
 * engaged -- hovered, focused, or tapped -- instead of waiting hidden behind
 * a CSS opacity rule. The history list is published as exactly one clickable
 * button per conversation, and mounting the actions on engagement keeps that
 * contract intact while revealing them for pointer, keyboard, and touch
 * users alike.
 */

import { useRef, useState } from "react";
import {
  Plus,
  Shield,
  LogOut,
  MessageSquare,
  Pencil,
  Trash2,
  Check,
  X,
} from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "react-hot-toast";
import {
  ApiError,
  deleteConversation,
  getConversations,
  renameConversation,
} from "@/lib/api";
import { ConversationMeta, formatRelativeTimestamp } from "@/types/chat";
import { useAuth } from "@/context/AuthContext";

interface SidebarProps {
  /** Callback when the user clicks "New Chat". */
  onNewChat: () => void;
  /** Optional callback when the user clicks logout. */
  onLogout?: () => void;
  /** Callback when a conversation is selected from history. */
  onSelectChat?: (id: string) => void;
  /** Incremented to trigger a refetch of conversation history. */
  refreshTrigger?: number;
  /** The currently active conversation ID, if any. */
  activeConversationId?: string | null;
  /** Callback after a conversation is deleted, carrying its id. */
  onDeleteConversation?: (id: string) => void;
}

interface ConversationRowProps {
  /** The conversation this row represents. */
  chat: ConversationMeta;
  /** Whether this row is the conversation currently open. */
  isActive: boolean;
  /** The instant every relative timestamp in the list is measured from. */
  now: Date;
  /** Renames the conversation, reporting a failure through its toast. */
  onRename: (id: string, title: string) => void;
  /** Deletes the conversation, reporting a failure through its toast. */
  onDelete: (id: string) => void;
  /** Selects the conversation as the active transcript. */
  onSelect: (id: string) => void;
}

/** Turn a rejection into the sentence its toast should show. */
function failureMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError && error.message ? error.message : fallback;
}

/**
 * One history row: the selectable conversation plus its rename and delete
 * actions.
 *
 * The editing and confirming state lives here rather than in the list so one
 * row can be renamed or confirmed at a time without each render re-deriving
 * the state of every other row.
 */
function ConversationRow({
  chat,
  isActive,
  now,
  onRename,
  onDelete,
  onSelect,
}: ConversationRowProps) {
  const [actionsVisible, setActionsVisible] = useState(false);
  // A null draft is "not renaming". The draft is seeded from the title at the
  // moment the editor opens rather than mirrored into it, so a refetch that
  // renames the conversation elsewhere can never leave a stale draft behind.
  const [draftTitle, setDraftTitle] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const rowRef = useRef<HTMLDivElement>(null);

  const revealActions = () => setActionsVisible(true);

  const hideActions = () => {
    // Hiding the row the keyboard is sitting in would strand a focused action
    // button somewhere invisible, so an engaged row keeps its controls.
    const focused = document.activeElement;
    if (!rowRef.current?.contains(focused)) {
      setActionsVisible(false);
    }
  };

  const startRename = () => setDraftTitle(chat.title);

  const cancelRename = () => setDraftTitle(null);

  const submitRename = () => {
    const title = draftTitle?.trim() ?? "";
    if (!title) return;
    setDraftTitle(null);
    onRename(chat.id, title);
  };

  return (
    <div
      ref={rowRef}
      className="relative flex items-center rounded-xl transition-colors duration-200"
      onPointerEnter={revealActions}
      onPointerLeave={hideActions}
      onFocus={revealActions}
      onBlur={hideActions}
      data-testid={`conversation-row-${chat.id}`}
    >
      {draftTitle !== null ? (
        <form
          className="flex w-full items-center gap-1.5 px-2 py-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            submitRename();
          }}
        >
          <input
            autoFocus
            value={draftTitle}
            onChange={(event) => setDraftTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") cancelRename();
            }}
            aria-label={`New title for ${chat.title}`}
            className="min-w-0 flex-1 rounded-lg border border-insurance-border bg-insurance-surface px-2 py-1 text-sm text-insurance-ink placeholder:text-insurance-ink-tertiary focus:outline-none focus-visible:ring-2 focus-visible:ring-insurance-info/40"
            data-testid={`rename-input-${chat.id}`}
          />
          <button
            type="submit"
            disabled={!draftTitle.trim()}
            aria-label="Save title"
            className="rounded-lg border border-insurance-border bg-insurance-surface p-1.5 text-insurance-success shadow-soft transition-colors hover:border-insurance-border-strong disabled:cursor-not-allowed disabled:opacity-50"
            data-testid={`rename-save-${chat.id}`}
          >
            <Check className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={cancelRename}
            aria-label="Cancel rename"
            className="rounded-lg border border-insurance-border bg-insurance-surface p-1.5 text-insurance-ink-secondary shadow-soft transition-colors hover:border-insurance-border-strong hover:text-insurance-ink"
            data-testid={`rename-cancel-${chat.id}`}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </form>
      ) : confirmingDelete ? (
        <div
          className="w-full rounded-xl border border-insurance-error/40 bg-insurance-error/5 px-3 py-2"
          data-testid={`delete-confirmation-${chat.id}`}
        >
          <div className="text-xs text-insurance-ink">
            Delete this conversation?
          </div>
          <div className="mt-1.5 flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                setConfirmingDelete(false);
                onDelete(chat.id);
              }}
              className="rounded-lg border border-insurance-error/40 px-2.5 py-1 text-[11px] font-medium text-insurance-error transition-colors hover:bg-insurance-error/10"
              data-testid={`delete-confirm-${chat.id}`}
            >
              Delete
            </button>
            <button
              type="button"
              onClick={() => setConfirmingDelete(false)}
              className="rounded-lg border border-insurance-border px-2.5 py-1 text-[11px] font-medium text-insurance-ink-secondary transition-colors hover:text-insurance-ink"
              data-testid={`delete-cancel-${chat.id}`}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <>
          <button
            type="button"
            onClick={() => onSelect(chat.id)}
            aria-current={isActive ? "page" : undefined}
            className={`w-full flex items-center gap-3 px-3 py-2.5 pr-12 text-sm text-left rounded-xl transition-colors duration-200 group ${
              isActive
                ? "bg-insurance-border text-insurance-ink"
                : "hover:bg-insurance-surface-secondary text-insurance-ink-secondary hover:text-insurance-ink"
            }`}
            data-testid={`conversation-button-${chat.id}`}
          >
            <MessageSquare
              className={`w-4 h-4 flex-shrink-0 transition-colors ${
                isActive
                  ? "text-insurance-info"
                  : "text-insurance-ink-tertiary group-hover:text-insurance-info"
              }`}
            />
            <div className="flex-1 min-w-0">
              <div className="truncate">{chat.title}</div>
              <div className="text-[11px] text-insurance-ink-tertiary truncate">
                {formatRelativeTimestamp(new Date(chat.updated_at), now)}
              </div>
            </div>
          </button>
          {actionsVisible && (
            <div
              className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1"
              data-testid={`conversation-actions-${chat.id}`}
            >
              <button
                type="button"
                onClick={startRename}
                aria-label={`Rename ${chat.title}`}
                className="rounded-lg border border-insurance-border bg-insurance-surface p-1.5 text-insurance-ink-secondary shadow-soft transition-all hover:text-insurance-ink"
                data-testid={`rename-button-${chat.id}`}
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={() => setConfirmingDelete(true)}
                aria-label={`Delete ${chat.title}`}
                className="rounded-lg border border-insurance-border bg-insurance-surface p-1.5 text-insurance-error shadow-soft transition-all hover:border-insurance-error/40"
                data-testid={`delete-button-${chat.id}`}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function Sidebar({
  onNewChat,
  onLogout,
  onSelectChat,
  refreshTrigger = 0,
  activeConversationId,
  onDeleteConversation,
}: SidebarProps) {
  const { token } = useAuth();
  const queryClient = useQueryClient();
  const [searchTerm, setSearchTerm] = useState("");

  const { data } = useQuery({
    queryKey: ["conversations", refreshTrigger, searchTerm],
    queryFn: () => getConversations(token!, searchTerm),
    enabled: !!token,
  });

  const chats: ConversationMeta[] = data?.conversations || [];
  // One clock per render keeps every relative label in the list measured from
  // the same instant.
  const now = new Date();

  const handleRename = async (id: string, title: string) => {
    try {
      await renameConversation(token!, id, title);
      // The prefix is deliberately broader than the key: a filter in play adds
      // a term to the key, and every one of those lists is now stale.
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      toast.success("Conversation renamed");
    } catch (error) {
      toast.error(
        failureMessage(
          error,
          "We could not rename the conversation. Please try again.",
        ),
      );
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteConversation(token!, id);
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      onDeleteConversation?.(id);
      toast.success("Conversation deleted");
    } catch (error) {
      toast.error(
        failureMessage(
          error,
          "We could not delete the conversation. Please try again.",
        ),
      );
    }
  };

  return (
    <div
      id="sidebar"
      className="w-full h-full bg-insurance-surface flex flex-col text-insurance-ink"
      data-testid="sidebar"
      aria-label="Navigation sidebar"
    >
      <div
        id="sidebar-header"
        className="p-5 pb-4"
        data-testid="sidebar-header"
      >
        <div className="flex items-center gap-3">
          <div
            className="w-9 h-9 rounded-xl bg-insurance-info text-white flex items-center justify-center"
            aria-hidden="true"
          >
            <Shield className="w-5 h-5" />
          </div>
          <div className="leading-tight">
            <div
              id="sidebar-title"
              className="font-semibold text-[15px] tracking-tight"
              data-testid="sidebar-title"
            >
              OmniCare
            </div>
            <div className="text-xs text-insurance-ink-secondary">
              Policy &amp; Claims Assistant
            </div>
          </div>
        </div>
      </div>

      <div className="px-3 pb-2">
        <button
          id="new-chat-button"
          type="button"
          onClick={onNewChat}
          className="w-full flex items-center justify-center gap-2 px-3 py-2.5 rounded-xl bg-insurance-info text-white text-sm font-medium hover:bg-insurance-info-hover transition-all duration-200"
          data-testid="new-chat-button"
          aria-label="Start a new chat"
        >
          <Plus className="w-4 h-4" />
          <span data-testid="new-chat-label">New Chat</span>
        </button>
      </div>

      <div className="px-3 pb-2">
        <input
          type="search"
          value={searchTerm}
          onChange={(event) => setSearchTerm(event.target.value)}
          placeholder="Search conversations"
          aria-label="Search conversations"
          className="w-full rounded-xl border border-insurance-border bg-insurance-surface px-3 py-2 text-sm text-insurance-ink placeholder:text-insurance-ink-tertiary focus:outline-none focus-visible:ring-2 focus-visible:ring-insurance-info/40"
          data-testid="conversation-search"
        />
      </div>

      <div
        id="chat-history"
        className="flex-1 overflow-y-auto px-3 py-2 space-y-0.5"
        data-testid="chat-history"
        aria-label="Chat history"
      >
        <div className="px-2 py-2 text-xs font-medium text-insurance-ink-secondary">
          Conversations
        </div>
        {chats.length === 0 && (
          <div
            className="px-2 py-6 text-center"
            data-testid={
              searchTerm ? "no-conversations-match" : "no-conversations"
            }
          >
            <div className="text-xs text-insurance-ink-secondary">
              {searchTerm ? "No conversations match" : "No conversations yet"}
            </div>
            <div className="text-[11px] text-insurance-ink-tertiary mt-1">
              {searchTerm
                ? "Try a different search term."
                : "Your conversations will appear here."}
            </div>
          </div>
        )}
        {chats.map((chat) => (
          <ConversationRow
            key={chat.id}
            chat={chat}
            isActive={chat.id === activeConversationId}
            now={now}
            onRename={handleRename}
            onDelete={handleDelete}
            onSelect={(id) => onSelectChat?.(id)}
          />
        ))}
      </div>

      {onLogout && (
        <div className="p-3 pt-2">
          <button
            type="button"
            onClick={onLogout}
            className="w-full flex items-center justify-center gap-2 px-3 py-2.5 rounded-xl text-insurance-error hover:bg-insurance-error/10 text-sm font-medium transition-colors duration-200"
          >
            <LogOut className="w-4 h-4" />
            <span>Logout</span>
          </button>
        </div>
      )}

      <div
        id="sidebar-footer"
        className="px-5 py-3 border-t border-insurance-border text-[11px] text-insurance-ink-tertiary"
        data-testid="sidebar-footer"
      >
        OmniCare Policy &amp; Claims Assistant v1.0
      </div>
    </div>
  );
}
