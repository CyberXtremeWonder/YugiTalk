import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ErrorBoundary } from "./shared/ErrorBoundary";
import { installRuntimeMonitoring } from "./shared/monitoring";
import "./shared/styles.css";
import "./shared/hermes.css";

installRuntimeMonitoring();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
