import { describe, it, expect, vi, beforeEach } from "vitest";
import { ComponentProps } from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import AuthModal from "@/components/AuthModal";
import SignupPage from "@/pages/SignupPage";
import LoginPage from "@/pages/LoginPage";
import App from "@/App";
import { useAuth } from "@/context/AuthContext";

type AuthStatus = "loading" | "authenticated" | "unauthenticated";

vi.mock("react-hot-toast", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/pages/ChatPage", () => ({
  default: () => <div data-testid="chat-page" />,
}));

const { login } = vi.hoisted(() => ({ login: vi.fn() }));

vi.mock("@/context/AuthContext", () => ({
  useAuth: vi.fn(),
}));

const SIGNUP_TOKEN = "signup-access-token";
const SIGNUP_USER_ID = "user-456";

/** Answers the sign-up call with a working session and anything else with 200. */
function installFetch(): void {
  global.fetch = vi.fn((url: string) => {
    if (url.includes("/api/v1/auth/signup")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            access_token: SIGNUP_TOKEN,
            user_id: SIGNUP_USER_ID,
          }),
      } as Response);
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ user_id: SIGNUP_USER_ID }),
    } as Response);
  }) as unknown as typeof fetch;
}

function mockAuthStatus(status: AuthStatus): void {
  vi.mocked(useAuth).mockReturnValue({
    status,
    token: null,
    userId: null,
    login,
    logout: vi.fn(),
  });
}

function renderAuthModal(
  props: Partial<ComponentProps<typeof AuthModal>> = {},
) {
  const onAuthenticated = vi.fn();
  const onToggleMode = vi.fn();
  render(
    <AuthModal
      isLogin
      onAuthenticated={onAuthenticated}
      onToggleMode={onToggleMode}
      {...props}
    />,
  );
  return { onAuthenticated, onToggleMode };
}

function submitButtonName(isLogin: boolean): string {
  return isLogin ? "Sign in" : "Create account";
}

function submitForm(): HTMLFormElement {
  // Dispatched directly: jsdom blocks a click on submit while a required field
  // is empty, and the point here is our own validation, not the browser's.
  const form = screen.getByRole("main").querySelector("form");
  if (!form) throw new Error("Auth form was not rendered inside main");
  fireEvent.submit(form);
  return form;
}

/** Fills the sign-up form with matching passwords unless told otherwise. */
function fillSignupForm(
  values: {
    email?: string;
    password?: string;
    confirmPassword?: string;
  } = {},
): void {
  const {
    email = "new@example.com",
    password = "secret123",
    confirmPassword = "secret123",
  } = values;
  fireEvent.change(screen.getByLabelText("Email"), {
    target: { value: email },
  });
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: password },
  });
  fireEvent.change(screen.getByLabelText("Confirm password"), {
    target: { value: confirmPassword },
  });
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
}

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
  installFetch();
  mockAuthStatus("unauthenticated");
});

describe("AuthModal field semantics", () => {
  it("associates every field with its label", () => {
    renderAuthModal({ isLogin: false });

    expect(screen.getByLabelText("Email")).toHaveAttribute("id", "auth-email");
    expect(screen.getByLabelText("Password")).toHaveAttribute(
      "id",
      "auth-password",
    );
    expect(screen.getByLabelText("Confirm password")).toHaveAttribute(
      "id",
      "auth-confirm-password",
    );
  });

  it("hides the confirm field in sign-in mode but keeps the other two labelled", () => {
    renderAuthModal({ isLogin: true });

    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.queryByLabelText("Confirm password")).not.toBeInTheDocument();
  });

  it("renders as a page landmark with a single top-level heading", () => {
    renderAuthModal();

    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Sign in to OmniCare",
    );
    expect(screen.getAllByRole("heading")).toHaveLength(1);
  });

  it("focuses the email input on mount", () => {
    renderAuthModal();

    expect(screen.getByLabelText("Email")).toHaveFocus();
  });

  it("marks invalid fields on submit and links them to their message", () => {
    renderAuthModal({ isLogin: false });

    submitForm();

    const email = screen.getByLabelText("Email");
    expect(email).toHaveAttribute("aria-invalid", "true");
    expect(email).toHaveAttribute("aria-describedby", "auth-email-error");
    expect(document.getElementById("auth-email-error")).toHaveTextContent(
      "Email is required.",
    );

    const password = screen.getByLabelText("Password");
    expect(password).toHaveAttribute("aria-invalid", "true");
    expect(password).toHaveAttribute("aria-describedby", "auth-password-error");

    expect(screen.getByLabelText("Confirm password")).not.toHaveAttribute(
      "aria-invalid",
    );
  });

  it("reports a password mismatch on the confirm field", () => {
    renderAuthModal({ isLogin: false });

    fillSignupForm({ confirmPassword: "something-else" });
    submitForm();

    const confirm = screen.getByLabelText("Confirm password");
    expect(confirm).toHaveAttribute("aria-invalid", "true");
    expect(confirm).toHaveAttribute(
      "aria-describedby",
      "auth-confirm-password-error",
    );
    expect(
      document.getElementById("auth-confirm-password-error"),
    ).toHaveTextContent("Passwords do not match.");
  });

  it("clears aria-invalid and aria-describedby as soon as the field is edited", () => {
    renderAuthModal({ isLogin: false });

    submitForm();

    const email = screen.getByLabelText("Email");
    expect(email).toHaveAttribute("aria-invalid", "true");

    fireEvent.change(email, { target: { value: "new@example.com" } });

    expect(email).not.toHaveAttribute("aria-invalid");
    expect(email).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByText("Email is required.")).not.toBeInTheDocument();
  });

  it("does not submit while a field is invalid", async () => {
    const { onAuthenticated } = renderAuthModal({ isLogin: false });

    submitForm();

    await Promise.resolve();
    expect(onAuthenticated).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

describe("SignupPage auto-authentication", () => {
  it("adopts the signup session and lands on /chat, not /login", async () => {
    render(
      <MemoryRouter initialEntries={["/signup"]}>
        <LocationProbe />
        <Routes>
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/chat"
            element={<div data-testid="chat-destination" />}
          />
        </Routes>
      </MemoryRouter>,
    );

    fillSignupForm();
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() =>
      expect(login).toHaveBeenCalledWith(SIGNUP_TOKEN, SIGNUP_USER_ID),
    );
    await waitFor(() =>
      expect(screen.getByTestId("chat-destination")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("current-path").textContent).toBe("/chat");
    expect(screen.queryByLabelText("Email")).not.toBeInTheDocument();
  });

  it("keeps the sign-in page behaviour unchanged", async () => {
    const { onAuthenticated, onToggleMode } = renderAuthModal({
      isLogin: true,
    });

    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "new@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "secret123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(onAuthenticated).toHaveBeenCalledTimes(1));
    expect(onToggleMode).not.toHaveBeenCalled();
  });
});

describe("App catch-all route", () => {
  it("sends an unknown URL to the auth-appropriate page", async () => {
    window.history.pushState({}, "", "/nonexistent-page");

    render(<App />);

    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
        "Sign in to OmniCare",
      ),
    );
    expect(window.location.pathname).toBe("/login");

    window.history.pushState({}, "", "/");
  });

  it("sends an authenticated user with an unknown URL to /chat", async () => {
    window.history.pushState({}, "", "/nonexistent-page");
    mockAuthStatus("authenticated");

    render(<App />);

    await waitFor(() =>
      expect(screen.getByTestId("chat-page")).toBeInTheDocument(),
    );
    expect(window.location.pathname).toBe("/chat");

    window.history.pushState({}, "", "/");
  });
});
