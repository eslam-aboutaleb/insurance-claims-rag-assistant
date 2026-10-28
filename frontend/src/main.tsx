import ReactDOM from "react-dom/client";
import { Toaster } from "react-hot-toast";
import Providers from "./providers";
import App from "./App";
import "./globals.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <Providers>
    <App />
    <Toaster
      position="top-right"
      toastOptions={{
        style: {
          background: "rgb(var(--color-surface))",
          color: "rgb(var(--color-text))",
          border: "1px solid rgb(var(--color-border))",
          boxShadow: "0 4px 16px rgba(0,0,0,0.05)",
        },
      }}
    />
  </Providers>,
);
