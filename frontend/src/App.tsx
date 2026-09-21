import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import { api, primeCsrf } from "./lib/api";
import { useResource, useTheme } from "./lib/hooks";
import { DashboardSummary, GatewayStatus, User } from "./lib/types";
import { ToastHost, useToast } from "./components/ui";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { Domains } from "./pages/Domains";
import { Backends } from "./pages/Backends";
import { RoutesPage } from "./pages/RoutesPage";
import { Certificates } from "./pages/Certificates";
import { Cache } from "./pages/Cache";
import { Deploys } from "./pages/Deploys";
import { Security } from "./pages/Security";
import { TotpSetup } from "./pages/TotpSetup";

function Mark() {
  return (
    <svg className="rail__mark" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="var(--ink-700)" />
      <path
        d="M5 16h6m10 0h6"
        stroke="var(--signal)"
        strokeWidth="2.4"
        strokeLinecap="round"
      />
      <circle cx="16" cy="16" r="4.2" fill="none" stroke="var(--hit)" strokeWidth="2.4" />
    </svg>
  );
}

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/domains", label: "Domains" },
  { to: "/routes", label: "Routes" },
  { to: "/backends", label: "Backends" },
  { to: "/certificates", label: "Certificates" },
  { to: "/cache", label: "Cache" },
  { to: "/deploys", label: "Deploys" },
  { to: "/security", label: "Security" },
] as const;

/**
 * Sits in the rail and answers the question an operator asks before making any
 * change: is what is running right now actually valid?
 */
function ConfigState() {
  const { data } = useResource<GatewayStatus>("/api/v1/gateway/status/", 20000);
  if (!data) return null;

  if (!data.config_valid) {
    return (
      <span style={{ color: "var(--down)", display: "flex", gap: 6, alignItems: "center" }}>
        <span className="dot dot--failing" />
        Live config is invalid
      </span>
    );
  }

  const failed = data.last_deploy?.status === "failed";
  return (
    <span
      style={{
        color: failed ? "var(--warn)" : "var(--text-faint)",
        display: "flex",
        gap: 6,
        alignItems: "center",
      }}
    >
      <span className={failed ? "dot dot--draining" : "dot"} />
      {failed ? "Last deploy failed" : "Config live"}
    </span>
  );
}

function Shell({ user, onSignOut }: { user: User; onSignOut: () => void }) {
  const { data: summary } = useResource<DashboardSummary>(
    "/api/v1/monitoring/summary/",
    15000,
  );
  const [theme, toggleTheme] = useTheme();

  const counts: Record<string, number | undefined> = {
    "/domains": summary?.domains.total,
    "/routes": summary?.routing.total_rules,
    "/backends": summary?.backends.total,
    "/certificates": summary?.certificates.total,
  };

  return (
    <div className="shell">
      <nav className="rail">
        <div className="rail__brand">
          <Mark />
          <div>
            <div className="rail__name">Gateway</div>
            <div className="rail__host">{window.location.hostname}</div>
          </div>
        </div>

        <div className="rail__nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={"end" in item ? item.end : false}
              className="rail__link"
            >
              {item.label}
              {counts[item.to] !== undefined && (
                <span className="rail__count">{counts[item.to]}</span>
              )}
            </NavLink>
          ))}
        </div>

        <div className="rail__foot">
          <ConfigState />
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className="mono" style={{ flex: 1, overflow: "hidden" }}>
              {user.username}
            </span>
            <button
              className="btn btn--ghost btn--small"
              onClick={toggleTheme}
              aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
            >
              {theme === "dark" ? "☀" : "☾"}
            </button>
            <button className="btn btn--ghost btn--small" onClick={onSignOut}>
              Sign out
            </button>
          </div>
        </div>
      </nav>

      <main className="main">
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/domains" element={<Domains />} />
          <Route path="/routes" element={<RoutesPage />} />
          <Route path="/backends" element={<Backends />} />
          <Route path="/certificates" element={<Certificates />} />
          <Route path="/cache" element={<Cache />} />
          <Route path="/deploys" element={<Deploys />} />
          <Route path="/security" element={<Security />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function AuthGate() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const toast = useToast();

  useEffect(() => {
    void (async () => {
      await primeCsrf();
      try {
        setUser(await api.get<User>("/api/v1/auth/me/"));
      } catch {
        setUser({ authenticated: false });
      } finally {
        setChecking(false);
      }
    })();
  }, []);

  useEffect(() => {
    const handler = () => setUser((current) =>
      current ? { ...current, totp_setup_required: true } : current,
    );
    window.addEventListener("gateway:totp-setup-required", handler);
    return () => window.removeEventListener("gateway:totp-setup-required", handler);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api.post("/api/v1/auth/logout/");
    } finally {
      setUser({ authenticated: false });
      toast("Signed out.");
    }
  }, [toast]);

  if (checking) {
    return (
      <div className="login">
        <div style={{ color: "var(--text-faint)", fontSize: "0.85rem" }}>Connecting…</div>
      </div>
    );
  }

  if (!user?.authenticated) {
    return <Login onSignedIn={setUser} />;
  }

  // Policy can require a second factor at any time, including for accounts that
  // already existed. Enrollment therefore stands in front of the whole panel
  // rather than being something an operator could put off.
  if (user.totp_setup_required) {
    return (
      <TotpSetup
        onComplete={() => setUser({ ...user, totp_setup_required: false })}
      />
    );
  }

  return <Shell user={user} onSignOut={signOut} />;
}

export function App() {
  return (
    <ToastHost>
      <AuthGate />
    </ToastHost>
  );
}
