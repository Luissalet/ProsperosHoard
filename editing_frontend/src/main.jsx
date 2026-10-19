import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./hoard-theme.css";
import "./index.css";
import "./hoard-copilot.js";

createRoot(document.getElementById("root")).render(<App />);

// PWA: register the service worker so the app can be installed. Only over HTTPS or localhost; never blocks the app.
if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1")) {
  try {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/editing/sw.js", {scope:'/editing/'}).catch(() => {});
    });
  } catch {
    // installing is optional
  }
}
