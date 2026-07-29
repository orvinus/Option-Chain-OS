import React, { Suspense } from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { LoginGate } from "./components/LoginGate";
import "./index.css";

// Two isolated dashboards on one app/backend:
//   /            -> primary (Ayush-bhai) dashboard  (<App/>)
//   /hidden(...) -> Main-branch dashboard, lazy-loaded & code-split (./hidden/App)
// Only one tree ever mounts (ternary), so there is never a double auth poll or
// double /ws/oi-stream connection. SPA history-fallback (Vite dev + prod nginx
// try_files) already serves index.html for /hidden on hard refresh.
const path = window.location.pathname.replace(/\/+$/, ""); // strip trailing slash(es)
const isHidden = path === "/hidden" || path.startsWith("/hidden/"); // NOT /hiddenfoo
const HiddenApp = React.lazy(() => import("./hidden/App"));

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {isHidden ? (
      <Suspense fallback={null}>
        <HiddenApp />
      </Suspense>
    ) : (
      <LoginGate>
        <App />
      </LoginGate>
    )}
  </React.StrictMode>
);
