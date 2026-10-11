/**
 * Authentication context for the OmniCare frontend.
 *
 * Provides global access to the current user's authentication state, including
 * the JWT token, user ID, and login/logout methods. State is persisted to
 * ``localStorage`` so sessions survive page refreshes.
 *
 * Usage:
 *   Import ``useAuth`` in any client component to access the auth context:
 *     const { token, userId, status, login, logout } = useAuth();
 *
 * Lifecycle:
 *   - On mount, the context hydrates state from localStorage and validates
 *     the session via ``GET /api/v1/auth/me``.
 *   - ``status`` reflects the auth state: ``"loading"``, ``"authenticated"``,
 *     or ``"unauthenticated"``.
 *   - ``login`` stores the new token and user ID in both React state and
 *     localStorage.
 *   - ``logout`` clears both and triggers a toast notification whose wording
 *     depends on the reason: a deliberate sign-out reports success, while an
 *     expired session (a rejected hydration or a mid-session 401) reports the
 *     expiry instead.
 */

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  ReactNode,
} from "react";
import { toast } from "react-hot-toast";
import { getCurrentUser } from "@/lib/api";

/** A deliberate sign-out: the user asked for it. */
const LOGOUT_MESSAGE = "Logged out successfully";
/** The session died on its own; the user did not end it. */
const SESSION_EXPIRED_MESSAGE = "Session expired. Please sign in again.";

/** Why a session ended. Defaults to a deliberate user sign-out. */
export type LogoutReason = "user" | "expired";

interface AuthContextType {
  token: string | null;
  userId: string | null;
  status: "loading" | "authenticated" | "unauthenticated";
  login: (token: string, userId: string) => void;
  logout: (reason?: LogoutReason) => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => {
    const stored = localStorage.getItem("omnicare_token");
    if (stored && stored.includes(".")) {
      return stored;
    }
    if (stored) {
      localStorage.removeItem("omnicare_token");
    }
    return null;
  });
  const [userId, setUserId] = useState<string | null>(() => {
    const stored = localStorage.getItem("omnicare_user_id");
    if (!stored) return null;
    const uuid =
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    if (uuid.test(stored)) {
      return stored;
    }
    localStorage.removeItem("omnicare_user_id");
    return null;
  });
  const [status, setStatus] = useState<
    "loading" | "authenticated" | "unauthenticated"
  >("loading");

  const clearSession = useCallback(() => {
    localStorage.removeItem("omnicare_token");
    localStorage.removeItem("omnicare_user_id");
    setToken(null);
    setUserId(null);
    setStatus("unauthenticated");
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function hydrate() {
      try {
        await getCurrentUser(token);
        if (!cancelled) {
          setStatus("authenticated");
        }
      } catch {
        if (!cancelled) {
          // A stored token that the backend rejects is an expiry, not a
          // sign-out: say so instead of claiming a successful logout.
          if (token) {
            toast.error(SESSION_EXPIRED_MESSAGE, {
              id: SESSION_EXPIRED_MESSAGE,
            });
          }
          clearSession();
        }
      }
    }

    hydrate();

    return () => {
      cancelled = true;
    };
  }, [token, clearSession]);

  const login = (newToken: string, newUserId: string) => {
    localStorage.setItem("omnicare_token", newToken);
    localStorage.setItem("omnicare_user_id", newUserId);
    setToken(newToken);
    setUserId(newUserId);
    setStatus("authenticated");
  };

  const logout = async (reason: LogoutReason = "user") => {
    try {
      const apiUrl = import.meta.env.VITE_API_URL || "http://localhost:8000";
      await fetch(`${apiUrl}/api/v1/auth/logout`, {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // Best-effort logout; clear local state regardless.
    }

    clearSession();
    if (reason === "expired") {
      toast.error(SESSION_EXPIRED_MESSAGE, { id: SESSION_EXPIRED_MESSAGE });
      return;
    }
    toast.success(LOGOUT_MESSAGE, { id: LOGOUT_MESSAGE });
  };

  return (
    <AuthContext.Provider value={{ token, userId, status, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
