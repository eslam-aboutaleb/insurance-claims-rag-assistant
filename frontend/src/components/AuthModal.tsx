/**
 * AuthModal component.
 *
 * The authentication form for both sign-in and sign-up modes, controlled by the
 * ``isLogin`` prop. LoginPage and SignupPage render it as the whole page, so the
 * card is a ``main`` landmark rather than a floating dialog: there is nothing
 * behind it to dim or trap focus. Input is validated for shape and length
 * client-side before submitting to the backend auth endpoints.
 *
 * Props:
 *   isLogin: When true, renders sign-in fields; when false, renders sign-up fields.
 *   onAuthenticated: Callback receiving the access token and user ID after successful
 *     authentication. The parent uses this to update global auth state.
 *   onToggleMode: Callback invoked when the user clicks the toggle link to switch
 *     between sign-in and sign-up modes.
 */

import { useState } from "react";
import { toast } from "react-hot-toast";
import { Eye, EyeOff, Shield } from "lucide-react";

interface AuthModalProps {
  isLogin: boolean;
  onAuthenticated: (token: string, userId: string) => void;
  onToggleMode: () => void;
}

export default function AuthModal({
  isLogin,
  onAuthenticated,
  onToggleMode,
}: AuthModalProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<{
    email?: string;
    password?: string;
    confirmPassword?: string;
  }>({});

  const validateEmail = (value: string): string | undefined => {
    if (!value.trim()) return "Email is required.";
    const trimmed = value.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) {
      return "Enter a valid email address.";
    }
    return undefined;
  };

  const validatePassword = (value: string): string | undefined => {
    if (!value) return "Password is required.";
    if (value.length < 6) return "Password must be at least 6 characters.";
    return undefined;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const emailError = validateEmail(email);
    const passwordError = validatePassword(password);
    const confirmPasswordError = !isLogin
      ? password !== confirmPassword
        ? "Passwords do not match."
        : undefined
      : undefined;

    setFieldErrors({
      email: emailError,
      password: passwordError,
      confirmPassword: confirmPasswordError,
    });

    if (emailError || passwordError || confirmPasswordError) {
      return;
    }

    setIsLoading(true);
    const endpoint = isLogin ? "/api/v1/auth/signin" : "/api/v1/auth/signup";
    const apiUrl = import.meta.env.VITE_API_URL || "http://localhost:8000";

    try {
      const res = await fetch(`${apiUrl}${endpoint}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: email.trim(), password }),
      });

      const data = await res.json();

      if (!res.ok) {
        throw new Error(
          data.detail || data.error?.message || "Authentication failed",
        );
      }

      const successMsg = isLogin
        ? "Successfully signed in!"
        : "Successfully signed up!";
      toast.success(successMsg, { id: successMsg });
      onAuthenticated(data.access_token, data.user_id);
    } catch (err: any) {
      toast.error(err.message, { id: err.message });
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-insurance-bg z-50 flex items-center justify-center p-4">
      <main className="bg-insurance-surface border border-insurance-border rounded-2xl shadow-lifted w-full max-w-md max-h-[calc(100vh-2rem)] overflow-y-auto">
        <div className="p-6 md:p-8">
          <div className="mb-8 text-center">
            <div className="w-12 h-12 rounded-xl bg-insurance-info text-white flex items-center justify-center shadow-soft mx-auto mb-4">
              <Shield className="w-6 h-6" />
            </div>
            <h1 className="text-xl md:text-2xl font-semibold text-insurance-ink tracking-tight">
              {isLogin ? "Sign in to OmniCare" : "Create your OmniCare account"}
            </h1>
            <p className="mt-2 text-sm text-insurance-ink-secondary">
              {isLogin
                ? "Welcome back. Sign in to access your policies and claims."
                : "Sign up with your email to manage your policies and claims."}
            </p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label
                htmlFor="auth-email"
                className="block text-insurance-ink text-sm font-medium mb-2"
              >
                Email
              </label>
              <input
                id="auth-email"
                name="email"
                type="email"
                value={email}
                onChange={(e) => {
                  setEmail(e.target.value);
                  if (fieldErrors.email)
                    setFieldErrors((prev) => ({ ...prev, email: undefined }));
                }}
                aria-invalid={fieldErrors.email ? true : undefined}
                aria-describedby={
                  fieldErrors.email ? "auth-email-error" : undefined
                }
                className={`w-full bg-insurance-bg border ${
                  fieldErrors.email
                    ? "border-insurance-error"
                    : "border-insurance-border"
                } text-insurance-ink font-medium rounded-xl p-3.5 focus:outline-none focus:ring-4 focus:ring-insurance-info/10 focus:border-insurance-info transition-all shadow-subtle`}
                placeholder="Enter your email"
                required
                autoComplete="email"
                autoFocus
              />
              {fieldErrors.email && (
                <p
                  id="auth-email-error"
                  className="mt-1.5 text-xs text-insurance-error"
                >
                  {fieldErrors.email}
                </p>
              )}
            </div>
            <div>
              <label
                htmlFor="auth-password"
                className="block text-insurance-ink text-sm font-medium mb-2"
              >
                Password
              </label>
              <div className="relative">
                <input
                  id="auth-password"
                  name="password"
                  type={showPassword ? "text" : "password"}
                  value={password}
                  onChange={(e) => {
                    setPassword(e.target.value);
                    if (fieldErrors.password)
                      setFieldErrors((prev) => ({
                        ...prev,
                        password: undefined,
                      }));
                  }}
                  aria-invalid={fieldErrors.password ? true : undefined}
                  aria-describedby={
                    fieldErrors.password ? "auth-password-error" : undefined
                  }
                  className={`w-full bg-insurance-bg border ${
                    fieldErrors.password
                      ? "border-insurance-error"
                      : "border-insurance-border"
                  } text-insurance-ink font-medium rounded-xl p-3.5 pr-10 focus:outline-none focus:ring-4 focus:ring-insurance-info/10 focus:border-insurance-info transition-all shadow-subtle`}
                  placeholder="Enter your password"
                  required
                  autoComplete={isLogin ? "current-password" : "new-password"}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-insurance-ink-secondary hover:text-insurance-ink transition-colors"
                  aria-label={showPassword ? "Hide password" : "Show password"}
                >
                  {showPassword ? (
                    <EyeOff className="w-4 h-4" />
                  ) : (
                    <Eye className="w-4 h-4" />
                  )}
                </button>
              </div>
              {fieldErrors.password && (
                <p
                  id="auth-password-error"
                  className="mt-1.5 text-xs text-insurance-error"
                >
                  {fieldErrors.password}
                </p>
              )}
            </div>

            {!isLogin && (
              <div>
                <label
                  htmlFor="auth-confirm-password"
                  className="block text-insurance-ink text-sm font-medium mb-2"
                >
                  Confirm password
                </label>
                <div className="relative">
                  <input
                    id="auth-confirm-password"
                    name="confirm-password"
                    type={showConfirmPassword ? "text" : "password"}
                    value={confirmPassword}
                    onChange={(e) => {
                      setConfirmPassword(e.target.value);
                      if (fieldErrors.confirmPassword)
                        setFieldErrors((prev) => ({
                          ...prev,
                          confirmPassword: undefined,
                        }));
                    }}
                    aria-invalid={
                      fieldErrors.confirmPassword ? true : undefined
                    }
                    aria-describedby={
                      fieldErrors.confirmPassword
                        ? "auth-confirm-password-error"
                        : undefined
                    }
                    className={`w-full bg-insurance-bg border ${
                      fieldErrors.confirmPassword
                        ? "border-insurance-error"
                        : "border-insurance-border"
                    } text-insurance-ink font-medium rounded-xl p-3.5 pr-10 focus:outline-none focus:ring-4 focus:ring-insurance-info/10 focus:border-insurance-info transition-all shadow-subtle`}
                    placeholder="Re-enter your password"
                    required
                    autoComplete="new-password"
                  />
                  <button
                    type="button"
                    onClick={() => setShowConfirmPassword(!showConfirmPassword)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-insurance-ink-secondary hover:text-insurance-ink transition-colors"
                    aria-label={
                      showConfirmPassword ? "Hide password" : "Show password"
                    }
                  >
                    {showConfirmPassword ? (
                      <EyeOff className="w-4 h-4" />
                    ) : (
                      <Eye className="w-4 h-4" />
                    )}
                  </button>
                </div>
                {fieldErrors.confirmPassword && (
                  <p
                    id="auth-confirm-password-error"
                    className="mt-1.5 text-xs text-insurance-error"
                  >
                    {fieldErrors.confirmPassword}
                  </p>
                )}
              </div>
            )}

            <button
              type="submit"
              disabled={isLoading}
              className="w-full bg-insurance-info text-white font-semibold p-3.5 rounded-xl shadow-soft hover:bg-insurance-info-hover hover:shadow-medium transition-all duration-200 disabled:opacity-50"
            >
              {isLoading
                ? "Please wait..."
                : isLogin
                  ? "Sign in"
                  : "Create account"}
            </button>
          </form>

          <div className="mt-6 text-center">
            <button
              onClick={onToggleMode}
              className="text-insurance-info hover:text-insurance-ink text-sm font-medium transition-colors"
            >
              {isLogin
                ? "Don't have an account? Sign up"
                : "Already have an account? Sign in"}
            </button>
          </div>
        </div>
      </main>
    </div>
  );
}
