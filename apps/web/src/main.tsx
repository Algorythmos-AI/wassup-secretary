import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AuthProvider } from "./auth/auth";
import { settingProblems } from "./config";
import { NotConfigured } from "./pages/NotConfigured";
import "./styles/tokens.css";

const problems = settingProblems();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {problems.length > 0 ? (
      <NotConfigured problems={problems} />
    ) : (
      <AuthProvider>
        <App />
      </AuthProvider>
    )}
  </StrictMode>,
);
