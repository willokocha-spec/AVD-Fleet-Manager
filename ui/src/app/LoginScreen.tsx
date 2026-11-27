import { useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError, getStoredToken } from "@/lib/api";
import { APP_NAME, UI_VERSION, daemonLabel, mocksEnabled } from "@/lib/config";
import {
  Button,
  Field,
  Icon,
  IconButton,
  InlineNotice,
  Input,
  StatusPill,
} from "@/lib/components";
import { useTheme } from "@/lib/theme";

import { useAuth } from "./AuthProvider";
import { useDaemonStatus } from "./DaemonStatus";
import { LogoMark } from "./LogoMark";

/**
 * Full-screen connect screen shown whenever there is no verified bearer token.
 * Also doubles as the daemon-reachability diagnostic before login.
 */
export function LoginScreen() {
  const { login } = useAuth();
  const daemon = useDaemonStatus();
  const { resolved, toggle } = useTheme();
  const [token, setTokenValue] = useState<string>(() => getStoredToken() ?? "");
  const [showToken, setShowToken] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const mockMode = mocksEnabled();

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const submit = async (value: string): Promise<void> => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(value);
    } catch (caught) {
      const message =
        caught instanceof ApiError
          ? [caught.message, caught.hint].filter(Boolean).join(" — ")
          : caught instanceof Error
            ? caught.message
            : "Could not authenticate with the daemon.";
      setError(message);
    } finally {
      setSubmitting(false);
    }
  };

  const onSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    void submit(token);
  };

  const daemonStatus =
    daemon.state === "checking"
      ? "checking"
      : daemon.state === "online"
        ? "online"
        : daemon.state === "degraded"
          ? "degraded"
          : "offline";

  return (
    <div className="auth-screen">
      <div className="auth-screen__glow" aria-hidden="true" />
      <header className="auth-screen__top">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true">
            <LogoMark />
          </span>
          <span className="brand__text">
            <span className="brand__name">{APP_NAME}</span>
            <span className="brand__tag">Operator console</span>
          </span>
        </div>
        <div className="auth-screen__top-actions">
          <StatusPill
            status={daemonStatus}
            label={
              daemon.state === "checking"
                ? "Checking daemon"
                : daemon.state === "online"
                  ? "Daemon online"
                  : daemon.state === "degraded"
                    ? "Daemon degraded"
                    : "Daemon offline"
            }
            pulse={daemon.state === "online"}
            size="md"
            title={daemon.message ?? `Version ${daemon.health?.version ?? UI_VERSION}`}
          />
          <IconButton
            label={`Switch to ${resolved === "dark" ? "light" : "dark"} theme`}
            icon={resolved === "dark" ? "sun" : "moon"}
            onClick={toggle}
          />
        </div>
      </header>

      <main className="auth-layout">
        <section className="auth-hero" aria-label="About AVD Fleet">
          <p className="auth-hero__eyebrow">Local-first Android device cloud</p>
          <h1 className="auth-hero__title">
            One host.
            <br />
            A fleet of Android VMs.
          </h1>
          <p className="auth-hero__lead">
            Deploy, isolate and automate up to 64 Android Virtual Devices from a single
            accelerated machine — with proxy isolation, OOBE control and scripted automation
            built in.
          </p>
          <ul className="auth-hero__points">
            <li>
              <Icon name="fleet" size={17} aria-hidden="true" /> Deploy a template across 5+ AVDs
            </li>
            <li>
              <Icon name="shield" size={17} aria-hidden="true" /> Per-instance authenticated proxies
            </li>
            <li>
              <Icon name="scripts" size={17} aria-hidden="true" /> adb, Python and Appium runs with
              live output
            </li>
            <li>
              <Icon name="monitor" size={17} aria-hidden="true" /> OOBE detection and golden-snapshot
              reset
            </li>
          </ul>
          <p className="auth-hero__footer">
            Runs entirely on <code>{daemonLabel()}</code> · v{UI_VERSION}
          </p>
        </section>

        <section className="auth-card" aria-labelledby="auth-title">
          <div className="auth-card__head">
            <span className="auth-card__icon" aria-hidden="true">
              <Icon name="key" size={20} />
            </span>
            <div>
              <h2 id="auth-title">Connect to {APP_NAME}</h2>
              <p>The daemon requires a bearer token for every API call.</p>
            </div>
          </div>

          {daemon.state === "offline" ? (
            <InlineNotice
              tone="danger"
              title="Daemon unreachable"
              action={
                <Button size="sm" variant="ghost" icon="refresh" onClick={() => void daemon.refresh()}>
                  Retry
                </Button>
              }
            >
              <p>
                {daemon.message ?? "No response from the daemon."} Start it with{" "}
                <code>avdfleet serve</code>, then retry.
              </p>
            </InlineNotice>
          ) : null}

          <form className="auth-form" onSubmit={onSubmit} noValidate>
            <Field
              label="Bearer token"
              hint="Pasted from the daemon's token file. Kept in this browser's local storage only."
              error={error}
              required
            >
              <Input
                ref={inputRef}
                type={showToken ? "text" : "password"}
                name="token"
                value={token}
                placeholder="Paste your token"
                autoComplete="off"
                spellCheck={false}
                leadingIcon="key"
                onChange={(event) => setTokenValue(event.target.value)}
                trailing={
                  <button
                    type="button"
                    className="ds-input-reveal"
                    onClick={() => setShowToken((current) => !current)}
                    aria-label={showToken ? "Hide token" : "Show token"}
                  >
                    <Icon name={showToken ? "eye-off" : "eye"} size={16} />
                  </button>
                }
              />
            </Field>
            <Button
              type="submit"
              variant="primary"
              size="lg"
              fullWidth
              icon="power"
              loading={submitting}
              disabled={token.trim().length === 0}
            >
              Connect to daemon
            </Button>
          </form>

          {mockMode ? (
            <div className="auth-card__mock">
              <InlineNotice tone="info" title="Mock mode is enabled">
                <p>
                  This development build answers API calls from an in-browser mock of the frozen
                  OpenAPI contract. A daemon is not required.
                </p>
              </InlineNotice>
              <Button
                variant="secondary"
                fullWidth
                icon="zap"
                loading={submitting}
                onClick={() => {
                  setTokenValue("dev-token");
                  void submit("dev-token");
                }}
              >
                Use mock development token
              </Button>
            </div>
          ) : null}

          <details className="auth-card__help">
            <summary>Where do I find the token?</summary>
            <p>
              The daemon writes its bearer token to a file in the AVD Fleet data directory. The file
              is owner-readable only (0600 on POSIX, ACL-restricted on Windows). Copy its contents
              into the field above.
            </p>
            <p>
              The token is stored in this browser as <code>localStorage["avdfleet.token"]</code> and
              sent as an <code>Authorization: Bearer</code> header. WebSocket events pass it as a
              query parameter because browsers cannot set headers on WebSocket handshakes.
            </p>
          </details>
        </section>
      </main>

      <footer className="auth-screen__footer">
        {APP_NAME} {UI_VERSION} · loopback-only by default · secrets stay on this host
      </footer>
    </div>
  );
}
