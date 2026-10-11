import { Navigate, useNavigate } from "react-router-dom";
import AuthModal from "@/components/AuthModal";
import { useAuth } from "@/context/AuthContext";

export default function SignupPage() {
  const { status, login } = useAuth();
  const navigate = useNavigate();

  if (status === "loading") return null;
  if (status === "authenticated") return <Navigate to="/chat" replace />;

  return (
    <AuthModal
      isLogin={false}
      onAuthenticated={(token, userId) => {
        // Signup returns a working session; adopting it avoids a forced re-login.
        login(token, userId);
        navigate("/chat", { replace: true });
      }}
      onToggleMode={() => navigate("/login")}
    />
  );
}
