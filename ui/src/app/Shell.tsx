import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";

import { APP_NAME, SIDEBAR_STORAGE_KEY, UI_VERSION, daemonLabel } from "@/lib/config";
import {
  Button,
  Icon,
  IconButton,
  InlineNotice,
  Spinner,
  StatusPill,
} from "@/lib/components";
import { useTheme } from "@/lib/theme";
import { findPage, navPages, pages } from "@/routes";

import { useAuth } from "./AuthProvider";
import { useDaemonStatus } from "./DaemonStatus";
import { useEventStream } from "./EventStream";
import { LoginScreen } from "./LoginScreen";
import { LogoMark } from "./LogoMark";
import { NotFoundPage } from "./NotFoundPage";
import { PageBoundary } from "./PageBoundary";

/** Root of the authenticated console: boot splash → connect screen → shell. */
export function AppShell() {
  const { status } = useAuth();
  if (status === "checking") return <BootSplash />;
  if (status === "anonymous") return <LoginScreen />;
  return <ConsoleShell />;
}

function BootSplash() {
  return (
    <div className="boot-splash" role="status" aria-live="polite">
      <span className="boot-splash__mark">
        <LogoMark size={40} />
      </span>
      <div className="boot-splash__text">
        <p className="boot-splash__title">Connecting to {APP_NAME}</p>
        <p className="boot-splash__hint">Verifying the bearer token with the daemon…</p>
      </div>
      <Spinner label="Connecting to the daemon" />
    </div>
  );
}

const LIVE_LABEL: Record<string, string> = {
  open: "Live events",
  connecting: "Connecting events…",
  reconnecting: "Reconnecting events…",
  closed: "Events offline",
  idle: "Events idle",
  disabled: "Events paused",
};

function ConsoleShell() {
  const location = useLocation();
  const auth = useAuth();
  const daemon = useDaemonStatus();
  const theme = useTheme();
  const { status: eventStatus } = useEventStream();
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [mobileOpen, setMobileOpen] = useState(false);
  const currentPage = findPage(location.pathname);

  useEffect(() => {
    setMobileOpen(false);
    window.scrollTo({ top: 0, behavior: "auto" });
  }, [location.pathname]);

  useEffect(() => {
    try {
      window.localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [collapsed]);

  useEffect(() => {
    if (!mobileOpen) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setMobileOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [mobileOpen]);

  const daemonChipStatus =
    daemon.state === "checking"
      ? "checking"
      : daemon.state === "online"
        ? "online"
        : daemon.state === "degraded"
          ? "degraded"
          : "offline";
  const daemonChipLabel =
    daemon.state === "checking"
      ? "Checking daemon"
      : daemon.state === "online"
        ? "Daemon online"
        : daemon.state === "degraded"
          ? "Daemon degraded"
          : "Daemon offline";

  return (
    <div className={`app-shell${collapsed ? " is-collapsed" : ""}${mobileOpen ? " is-drawer-open" : ""}`}>
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>

      <aside id="app-sidebar" className="sidebar" aria-label="Primary navigation">
        <div className="sidebar__brand">
          <NavLink to="/" className="brand" aria-label={`${APP_NAME} home`}>
            <span className="brand__mark" aria-hidden="true">
              <LogoMark />
            </span>
            <span className="brand__text">
              <span className="brand__name">{APP_NAME}</span>
              <span className="brand__tag">Operator console</span>
            </span>
          </NavLink>
          <IconButton
            className="sidebar__collapse"
            label={collapsed ? "Expand navigation" : "Collapse navigation"}
            icon={collapsed ? "chevron-right" : "chevron-left"}
            size="sm"
            onClick={() => setCollapsed((value) => !value)}
            aria-expanded={!collapsed}
            aria-controls="app-sidebar"
          />
        </div>

        <nav className="sidebar__nav" aria-label="Primary">
          <ul>
            {navPages.map(({ meta }) => (
              <li key={meta.path}>
                <NavLink
                  to={meta.path}
                  end={meta.path === "/"}
                  className={({ isActive }) => `nav-item${isActive ? " is-active" : ""}`}
                  title={meta.description ?? meta.title}
                >
                  <span className="nav-item__icon" aria-hidden="true">
                    <Icon name={meta.icon} size={18} />
                  </span>
                  <span className="nav-item__label">{meta.title}</span>
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="sidebar__footer">
          <div className="sidebar__live" title={`Events socket: ${eventStatus}`}>
            <span
              className={`live-dot${eventStatus === "open" ? " is-live" : ""}${
                eventStatus === "reconnecting" || eventStatus === "connecting" ? " is-waiting" : ""
              }`}
              aria-hidden="true"
            />
            <span>{LIVE_LABEL[eventStatus] ?? "Events"}</span>
          </div>
          <div className="sidebar__meta">
            <span>v{UI_VERSION}</span>
            <span aria-hidden="true">·</span>
            <span className="sidebar__host" title={daemonLabel()}>
              {daemon.health?.version ? `daemon ${daemon.health.version}` : "no daemon"}
            </span>
          </div>
        </div>
      </aside>

      {mobileOpen ? (
        <button
          type="button"
          className="sidebar-backdrop"
          aria-label="Close navigation"
          onClick={() => setMobileOpen(false)}
        />
      ) : null}

      <div className="app-main">
        <header className="topbar">
          <div className="topbar__left">
            <IconButton
              className="topbar__menu"
              label={mobileOpen ? "Close navigation" : "Open navigation"}
              icon={mobileOpen ? "close" : "menu"}
              onClick={() => setMobileOpen((value) => !value)}
              aria-expanded={mobileOpen}
              aria-controls="app-sidebar"
            />
            <span className="topbar__page">{currentPage?.meta.title ?? "Not found"}</span>
          </div>
          <div className="topbar__right">
            <button
              type="button"
              className="daemon-chip"
              onClick={() => void daemon.refresh()}
              title={
                daemon.message ??
                `Daemon ${daemon.state}${daemon.health ? ` · v${daemon.health.version}` : ""}${
                  daemon.lastCheckedAt ? ` · checked ${new Date(daemon.lastCheckedAt).toLocaleTimeString()}` : ""
                }`
              }
            >
              <StatusPill
                status={daemonChipStatus}
                label={daemonChipLabel}
                pulse={daemon.state === "online"}
                icon={false}
              />
            </button>
            <span className="topbar__auth">
              <StatusPill status="authenticated" label="Authenticated" icon="key" />
            </span>
            <IconButton
              label={`Switch to ${theme.resolved === "dark" ? "light" : "dark"} theme`}
              icon={theme.resolved === "dark" ? "sun" : "moon"}
              onClick={theme.toggle}
            />
            <IconButton label="Sign out" icon="logout" onClick={auth.signOut} />
          </div>
        </header>

        <main id="main-content" className="app-content" tabIndex={-1}>
          {daemon.state === "offline" ? (
            <InlineNotice
              className="app-banner"
              tone="danger"
              title="Daemon offline"
              action={
                <Button size="sm" variant="secondary" icon="refresh" onClick={() => void daemon.refresh()}>
                  Retry
                </Button>
              }
            >
              <p>
                {daemon.message ?? "Lost the connection to the AVD Fleet daemon."} Data on this page
                may be stale.
              </p>
            </InlineNotice>
          ) : null}
          <Routes>
            {pages.map(({ meta, Page }) => (
              <Route
                key={meta.path}
                path={meta.path}
                element={
                  <PageBoundary meta={meta}>
                    <Page />
                  </PageBoundary>
                }
              />
            ))}
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
