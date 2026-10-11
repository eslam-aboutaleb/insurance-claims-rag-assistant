/**
 * Sidebar component.
 *
 * Provides the persistent navigation rail on the left side of the chat
 * interface. Contains the app branding, a "New Chat" action, the user's
 * conversation history list, and a logout button.
 *
 * Data flow:
 *   - Conversation history is fetched via React Query using the
 *     ``getConversations`` API call, keyed on ``refreshTrigger`` so the
 *     parent can force a refetch after creating a new conversation.
 *   - Selecting a conversation calls ``onSelectChat`` with the conversation
 *     ID; the parent then switches the ChatWindow into read-only history mode.
 *   - The sidebar is hidden on mobile by default and toggled via the
 *     ``sidebarOpen`` state in the parent page component.
 */

import { Plus, Shield, LogOut, MessageSquare } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { getConversations } from "@/lib/api";
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
}

export default function Sidebar({
  onNewChat,
  onLogout,
  onSelectChat,
  refreshTrigger = 0,
  activeConversationId,
}: SidebarProps) {
  const { token } = useAuth();

  const { data } = useQuery({
    queryKey: ["conversations", refreshTrigger],
    queryFn: () => getConversations(token!),
    enabled: !!token,
  });

  const chats: ConversationMeta[] = data?.conversations || [];
  // One clock per render keeps every relative label in the list measured from
  // the same instant.
  const now = new Date();

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
          className="w-full flex items-center justify-center gap-2 px-3 py-2.5 rounded-xl bg-insurance-info text-white text-sm font-medium hover:bg-insurance-ink-secondary transition-all duration-200"
          data-testid="new-chat-button"
          aria-label="Start a new chat"
        >
          <Plus className="w-4 h-4" />
          <span data-testid="new-chat-label">New Chat</span>
        </button>
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
          <div className="px-2 py-6 text-center">
            <div className="text-xs text-insurance-ink-secondary">
              No conversations yet
            </div>
            <div className="text-[11px] text-insurance-ink-tertiary mt-1">
              Your conversations will appear here.
            </div>
          </div>
        )}
        {chats.map((chat) => {
          const isActive = chat.id === activeConversationId;
          return (
            <button
              key={chat.id}
              type="button"
              onClick={() => onSelectChat && onSelectChat(chat.id)}
              className={`w-full flex items-center gap-3 px-3 py-2.5 text-sm text-left rounded-xl transition-colors duration-200 group ${
                isActive
                  ? "bg-insurance-border text-insurance-ink"
                  : "hover:bg-insurance-surface-secondary text-insurance-ink-secondary hover:text-insurance-ink"
              }`}
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
          );
        })}
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
