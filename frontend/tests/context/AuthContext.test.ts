import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { toast } from "react-hot-toast";
import { AuthProvider, useAuth } from "@/context/AuthContext";

vi.mock("react-hot-toast", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const LOGGED_OUT_MESSAGE = "Logged out successfully";
const SESSION_EXPIRED_MESSAGE = "Session expired. Please sign in again.";

const JWT = "header.payload.signature";

/**
 * 401s a call made without a bearer token, and answers the stored session with
 * ``meStatus`` so a rejected hydration is reachable.
 */
function installFetch(meStatus = 200): void {
  global.fetch = vi.fn((url: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    if (url.includes("/api/v1/auth/me")) {
      const isStoredSession = headers.Authorization === `Bearer ${JWT}`;
      if (!isStoredSession || meStatus !== 200) {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: () => Promise.resolve({ detail: "Sign in to continue" }),
        } as Response);
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ user_id: "user-123" }),
      } as Response);
    }
    if (url.includes("/api/v1/auth/logout")) {
      return Promise.resolve({
        ok: true,
        status: 204,
        json: () => Promise.resolve({}),
      } as Response);
    }
    return Promise.resolve({
      ok: false,
      status: 401,
      json: () => Promise.resolve({ detail: "Sign in to continue" }),
    } as Response);
  }) as unknown as typeof fetch;
}

/** Renders the hook with a stored, valid-looking session already in place. */
function renderSignedIn() {
  const { result } = renderHook(() => useAuth(), {
    wrapper: AuthProvider,
  });
  return result;
}

describe("AuthContext", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.clearAllMocks();
    installFetch();
  });

  it("starts in loading state", () => {
    const { result } = renderHook(() => useAuth(), {
      wrapper: AuthProvider,
    });

    expect(result.current.status).toBe("loading");
  });

  it("hydrates to unauthenticated when no token", async () => {
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: "Sign in to continue" }),
      } as Response),
    );

    const { result } = renderHook(() => useAuth(), {
      wrapper: AuthProvider,
    });

    await waitFor(() => expect(result.current.status).not.toBe("loading"));

    expect(result.current.status).toBe("unauthenticated");
    expect(result.current.token).toBeNull();
    expect(result.current.userId).toBeNull();
  });

  it("login sets token, userId and authenticated status", async () => {
    global.fetch = vi.fn((url: string) => {
      if (url.includes("/api/v1/auth/me")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ user_id: "user-123" }),
        } as Response);
      }
      return Promise.resolve({
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: "Sign in to continue" }),
      } as Response);
    });

    const { result } = renderHook(() => useAuth(), {
      wrapper: AuthProvider,
    });

    await waitFor(() => expect(result.current.status).not.toBe("loading"));

    act(() => {
      result.current.login("fake-token", "user-123");
    });

    expect(result.current.token).toBe("fake-token");
    expect(result.current.userId).toBe("user-123");
    expect(result.current.status).toBe("authenticated");
  });

  it("logout clears state and sets unauthenticated", async () => {
    let meCallCount = 0;
    global.fetch = vi.fn((url: string, options?: RequestInit) => {
      if (url.includes("/api/v1/auth/me")) {
        meCallCount += 1;
        const authHeader = options?.headers?.["Authorization"];
        if (meCallCount > 1 && !authHeader) {
          return Promise.resolve({
            ok: false,
            status: 401,
            json: () => Promise.resolve({ detail: "Sign in to continue" }),
          } as Response);
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ user_id: "user-123" }),
        } as Response);
      }
      if (url.includes("/api/v1/auth/logout")) {
        return Promise.resolve({
          ok: true,
          status: 204,
          json: () => Promise.resolve({}),
        } as Response);
      }
      return Promise.resolve({
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: "Sign in to continue" }),
      } as Response);
    });

    const { result } = renderHook(() => useAuth(), {
      wrapper: AuthProvider,
    });

    await waitFor(() => expect(result.current.status).not.toBe("loading"));

    act(() => {
      result.current.login("fake-token", "user-123");
    });

    expect(result.current.status).toBe("authenticated");

    await act(async () => {
      await result.current.logout();
    });

    expect(result.current.token).toBeNull();
    expect(result.current.userId).toBeNull();
    expect(result.current.status).toBe("unauthenticated");
  });

  it("logout() without a reason keeps the successful sign-out toast", async () => {
    window.localStorage.setItem("omnicare_token", JWT);
    window.localStorage.setItem("omnicare_user_id", "user-123");

    const result = renderSignedIn();
    await waitFor(() => expect(result.current.status).toBe("authenticated"));

    await act(async () => {
      await result.current.logout();
    });

    expect(toast.success).toHaveBeenCalledWith(LOGGED_OUT_MESSAGE, {
      id: LOGGED_OUT_MESSAGE,
    });
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("logout('expired') reports the expiry instead of a sign-out", async () => {
    window.localStorage.setItem("omnicare_token", JWT);
    window.localStorage.setItem("omnicare_user_id", "user-123");

    const result = renderSignedIn();
    await waitFor(() => expect(result.current.status).toBe("authenticated"));

    await act(async () => {
      await result.current.logout("expired");
    });

    expect(toast.error).toHaveBeenCalledWith(SESSION_EXPIRED_MESSAGE, {
      id: SESSION_EXPIRED_MESSAGE,
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(result.current.token).toBeNull();
    expect(result.current.userId).toBeNull();
    expect(result.current.status).toBe("unauthenticated");
    expect(window.localStorage.getItem("omnicare_token")).toBeNull();
    expect(window.localStorage.getItem("omnicare_user_id")).toBeNull();
  });

  it("clears storage and reports expiry when hydration rejects a stored token", async () => {
    window.localStorage.setItem("omnicare_token", JWT);
    window.localStorage.setItem("omnicare_user_id", "user-123");
    installFetch(401);

    const result = renderSignedIn();

    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    expect(toast.error).toHaveBeenCalledWith(SESSION_EXPIRED_MESSAGE, {
      id: SESSION_EXPIRED_MESSAGE,
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(result.current.token).toBeNull();
    expect(result.current.userId).toBeNull();
    expect(window.localStorage.getItem("omnicare_token")).toBeNull();
    expect(window.localStorage.getItem("omnicare_user_id")).toBeNull();
  });

  it("does not report an expiry when there was no session to begin with", async () => {
    const result = renderSignedIn();

    await waitFor(() => expect(result.current.status).not.toBe("loading"));

    expect(result.current.status).toBe("unauthenticated");
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
  });
});
