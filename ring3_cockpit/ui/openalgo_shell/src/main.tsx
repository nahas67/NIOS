import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { SessionProvider } from "@/lib/session";
import { App } from "./App";
import "./index.css";

const container = document.getElementById("root");
if (!container) throw new Error("NIOS cockpit root element is missing");

createRoot(container).render(
  <StrictMode>
    <SessionProvider>
      <App />
    </SessionProvider>
  </StrictMode>,
);
