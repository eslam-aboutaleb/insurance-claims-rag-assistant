import { useEffect, useRef, useState } from "react";
import { Navigate, useParams, useNavigate } from "react-router-dom";
import Sidebar from "@/components/Sidebar";
import ChatWindow from "@/components/ChatWindow";
import { toast } from "react-hot-toast";
import { useAuth } from "@/context/AuthContext";
import { resetChat } from "@/lib/api";

/**
 * Width below which the sidebar covers the transcript instead of sitting
 * beside it.
 *
 * It has to match the ``md`` breakpoint the layout is already built on: the
 * off-canvas sidebar is hidden with a transform, which leaves nothing in the
 * DOM that says whether it is on screen, so the media query is the only
 * statement of that fact there is. If the two ever disagree, an element that is
 * visible gets marked inert and becomes unusable.
 */
const MOBILE_MEDIA_QUERY = "(max-width: 767px)";

/**
 * Read the phone-layout flag without assuming the environment has a media
 * engine.
 *
 * A jsdom-style runtime without ``matchMedia`` reports the wider layout, which
 * is the safe answer: it is the layout where neither the sidebar nor the
 * transcript is taken out of the accessibility tree.
 *
 * @returns True while the phone layout is active.
 */
function readIsMobileViewport(): boolean {
  if (
    typeof window === "undefined" ||
    typeof window.matchMedia !== "function"
  ) {
    return false;
  }
  return window.matchMedia(MOBILE_MEDIA_QUERY).matches;
}

/**
 * Whether the viewport is currently phone-width, kept current across resizes.
 *
 * @returns True while the phone layout is active.
 */
function useIsMobileViewport(): boolean {
  const [isMobile, setIsMobile] = useState(readIsMobileViewport);

  useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return;
    }
    const query = window.matchMedia(MOBILE_MEDIA_QUERY);
    const onChange = (event: MediaQueryListEvent) => setIsMobile(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  return isMobile;
}

export default function ChatPage() {
  const touchStartX = useRef<number | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [chatKey, setChatKey] = useState(0);
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const { status, logout, token } = useAuth();
  const { conversationId } = useParams<{ conversationId?: string }>();
  const navigate = useNavigate();
  const [historyLoadError, setHistoryLoadError] = useState<string | null>(null);
  const isMobileViewport = useIsMobileViewport();

  /**
   * The closed sidebar is off-canvas on a phone and a permanent rail on a wider
   * screen, so only the former is hidden from assistive technology and taken
   * out of the tab order: on desktop the same markup is fully visible.
   *
   * ``inert`` is passed as the empty string rather than a boolean because that
   * is the form the DOM attribute takes, and React renders it verbatim.
   */
  const sidebarIsOffCanvas = isMobileViewport && !sidebarOpen;
  const sidebarAttributes = sidebarIsOffCanvas
    ? ({ inert: "", "aria-hidden": "true" } as const)
    : {};

  /** Symmetrically, the transcript is behind the drawer while it is open on a phone. */
  const chatAttributes =
    isMobileViewport && sidebarOpen
      ? ({ inert: "", "aria-hidden": "true" } as const)
      : {};

  if (status === "loading") {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-insurance-info border-t-transparent rounded-full animate-spin" />
          <span className="text-sm text-insurance-ink-secondary">
            Loading OmniCare...
          </span>
        </div>
      </div>
    );
  }
  if (status !== "authenticated") return <Navigate to="/login" replace />;

  const handleNewChat = () => {
    resetChat(token).catch(() => {
      // Best-effort reset UI even if the API call fails.
    });
    setHistoryLoadError(null);
    setChatKey((prev) => prev + 1);
    setRefreshTrigger((prev) => prev + 1);
    toast.success("Started a new chat", { id: "Started a new chat" });
    if (sidebarOpen) setSidebarOpen(false);
    navigate("/chat", { replace: true });
  };

  const handleSelectChat = (id: string) => {
    setHistoryLoadError(null);
    setChatKey((prev) => prev + 1);
    if (sidebarOpen) setSidebarOpen(false);
    navigate(`/chat/${id}`);
  };

  const handleHistoryLoaded = () => {
    setHistoryLoadError(null);
  };

  const handleHistoryLoadError = (message: string) => {
    setHistoryLoadError(message);
  };

  const handleDeleteConversation = (id: string) => {
    // The transcript the user is reading is gone, so the page leaves it rather
    // than holding a view whose history would 404 on the next load.
    if (id !== conversationId) return;
    setHistoryLoadError(null);
    navigate("/chat", { replace: true });
  };

  return (
    <main
      id="app-main"
      className="flex h-full w-full overflow-hidden"
      data-testid="app-main"
    >
      <div
        id="sidebar-container"
        className={`fixed md:relative z-30 h-full md:w-[272px] flex-shrink-0 transition-transform duration-300 ease-in-out ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0"
        } w-[272px]`}
        data-testid="sidebar-container"
        {...sidebarAttributes}
        onTouchStart={(e) => {
          const touch = e.touches[0];
          touchStartX.current = touch.clientX;
        }}
        onTouchEnd={(e) => {
          const touch = e.changedTouches[0];
          if (touchStartX.current && touchStartX.current - touch.clientX > 50) {
            setSidebarOpen(false);
          }
        }}
      >
        <Sidebar
          onNewChat={handleNewChat}
          onLogout={logout}
          onSelectChat={handleSelectChat}
          refreshTrigger={refreshTrigger}
          activeConversationId={conversationId}
          onDeleteConversation={handleDeleteConversation}
        />
      </div>

      {sidebarOpen && (
        <div
          id="sidebar-overlay"
          className="fixed inset-0 bg-insurance-ink/15 backdrop-blur-sm z-10 md:hidden"
          onClick={() => setSidebarOpen(false)}
          data-testid="sidebar-overlay"
          aria-label="Close sidebar"
        />
      )}

      <div
        id="chat-container"
        className="flex-1 flex flex-col h-full relative"
        {...chatAttributes}
        data-testid="chat-container"
      >
        {/* Scoped to the chat area rather than the whole page: a failed history
            load must not blind the sidebar, because opening another
            conversation is how the reader gets out of it. It sits before the
            menu button so that, at equal stacking, the button still paints on
            top and keeps the drawer reachable. */}
        {historyLoadError && (
          <div
            className="absolute inset-0 flex items-center justify-center bg-insurance-bg/80 z-10"
            data-testid="history-load-error"
          >
            <div className="max-w-sm text-center p-4 bg-insurance-surface border border-insurance-border rounded-xl shadow-medium">
              <div className="text-sm font-medium text-insurance-ink mb-1">
                Failed to load conversation
              </div>
              <div className="text-xs text-insurance-ink-secondary mb-3">
                {historyLoadError}
              </div>
              <button
                onClick={() => navigate("/chat")}
                className="px-3 py-2 bg-insurance-info text-white text-xs font-medium rounded-lg hover:bg-insurance-info-hover transition-colors"
              >
                Start a new chat
              </button>
            </div>
          </div>
        )}
        <div
          id="mobile-menu-button"
          className="absolute top-0 left-0 p-4 md:hidden z-10"
          data-testid="mobile-menu-button"
        >
          <button
            id="mobile-menu-toggle"
            onClick={() => setSidebarOpen(true)}
            className="p-2 text-insurance-ink bg-insurance-surface border border-insurance-border rounded-lg shadow-subtle hover:shadow-soft transition-subtle"
            aria-label="Open menu"
            data-testid="mobile-menu-toggle"
          >
            <svg
              className="w-5 h-5"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.8}
                d="M4 6h16M4 12h16M4 18h16"
              />
            </svg>
          </button>
        </div>
        <ChatWindow
          key={`${conversationId ?? "new"}-${chatKey}`}
          onNewChat={handleNewChat}
          historyId={conversationId}
          onMessageSent={() => setRefreshTrigger((p) => p + 1)}
          onAuthError={() => logout("expired")}
          onHistoryLoaded={handleHistoryLoaded}
          onHistoryLoadError={handleHistoryLoadError}
        />
      </div>
    </main>
  );
}
