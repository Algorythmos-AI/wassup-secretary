import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AuthProvider } from "./auth/auth";
import { missingSettings } from "./config";
import { NotConfigured } from "./pages/NotConfigured";
import "./styles/tokens.css";

const missing = missingSettings();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {missing.length > 0 ? (
      <NotConfigured missing={missing} />
    ) : (
      <AuthProvider>
        <App />
      </AuthProvider>
    )}
  </StrictMode>,
);
