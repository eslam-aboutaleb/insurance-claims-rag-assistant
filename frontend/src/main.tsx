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
          background: "rgba(255, 255, 255, 0.85)",
          color: "#2b2926",
          border: "1px solid rgba(214, 207, 195, 0.5)",
          boxShadow: "0 4px 16px rgba(43,41,38,0.05)",
        },
      }}
    />
  </Providers>,
);
