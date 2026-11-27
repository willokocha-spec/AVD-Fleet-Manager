import { useCallback, useEffect, useRef, useState, type MouseEvent } from "react";

import {
  Button,
  EmptyState,
  IconButton,
  InlineNotice,
  Input,
  Spinner,
  StatusPill,
} from "@/lib/components";
import { ApiError, api } from "@/lib/api";
import type { InputRequest, InstanceState } from "@/lib/types";

/** Android key events exposed as one-click buttons next to the screen. */
const KEYS: ReadonlyArray<{ keycode: string; label: string }> = [
  { keycode: "KEYCODE_HOME", label: "Home" },
  { keycode: "KEYCODE_BACK", label: "Back" },
  { keycode: "KEYCODE_APP_SWITCH", label: "Recents" },
  { keycode: "KEYCODE_ENTER", label: "Enter" },
  { keycode: "KEYCODE_POWER", label: "Power" },
];

const DEFAULT_REFRESH_MS = 3_000;

export interface ScreenPreviewProps {
  /** Instance name used by the screenshot and input endpoints. */
  name: string;
  state: InstanceState;
  /** `AvdInfo.screen_available`; when explicitly false a hint is shown. */
  screenAvailable?: boolean;
  /** Auto-refresh interval (2–5 s while the instance is running). */
  autoRefreshMs?: number;
}

interface ImageSource {
  src: string;
  revoke: () => void;
}

/**
 * Read a Blob as a data URL — fallback for environments without object URLs.
 *
 * `Blob.arrayBuffer()` is missing from a few DOM implementations (jsdom's Blob,
 * for example), so a FileReader fallback keeps the preview working in tests.
 */
async function blobToDataUrl(blob: Blob): Promise<string> {
  const arrayBuffer = (blob as Blob & { arrayBuffer?: () => Promise<ArrayBuffer> }).arrayBuffer;
  if (typeof arrayBuffer === "function") {
    const buffer = await arrayBuffer.call(blob);
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return `data:${blob.type || "image/png"};base64,${btoa(binary)}`;
  }
  if (typeof FileReader !== "undefined") {
    return new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () =>
        typeof reader.result === "string"
          ? resolve(reader.result)
          : reject(new Error("The screenshot could not be decoded."));
      reader.onerror = () =>
        reject(reader.error ?? new Error("The screenshot could not be decoded."));
      reader.readAsDataURL(blob);
    });
  }
  throw new Error("This browser cannot display screenshots.");
}

async function createImageSource(blob: Blob): Promise<ImageSource> {
  try {
    if (typeof URL !== "undefined" && typeof URL.createObjectURL === "function") {
      const created: unknown = URL.createObjectURL(blob);
      // Some DOM implementations (notably jsdom) expose the method but return
      // undefined; fall back to a data URL so the preview always gets a source.
      if (typeof created === "string" && created.length > 0) {
        const src = created;
        return {
          src,
          revoke: () => {
            try {
              URL.revokeObjectURL(src);
            } catch {
              /* already revoked */
            }
          },
        };
      }
    }
  } catch {
    /* fall through to the data URL path */
  }
  return { src: await blobToDataUrl(blob), revoke: () => undefined };
}

function screenshotErrorMessage(caught: unknown): string {
  if (caught instanceof ApiError) {
    if (caught.status === 503) {
      return "The daemon cannot capture the screen yet — the emulator may still be booting.";
    }
    if (caught.status === 404) {
      return "The instance no longer exists (it may have been deleted).";
    }
    return caught.hint ? `${caught.message} (${caught.hint})` : caught.message;
  }
  return caught instanceof Error ? caught.message : String(caught);
}

function inputErrorMessage(caught: unknown): string {
  if (caught instanceof ApiError) {
    if (caught.status === 409) return "The daemon rejected the input because the instance is not running.";
    return caught.hint ? `${caught.message} (${caught.hint})` : caught.message;
  }
  return caught instanceof Error ? caught.message : String(caught);
}

interface PlaceholderCopy {
  title: string;
  description: string;
}

function placeholderFor(state: InstanceState): PlaceholderCopy {
  switch (state) {
    case "defined":
      return {
        title: "Screen unavailable — instance not started",
        description: "Every instance boots into a black screen until it is started. Use Start to power it on.",
      };
    case "stopped":
      return {
        title: "Screen unavailable — instance stopped",
        description: "Start the instance to stream its screen again.",
      };
    case "starting":
    case "booting":
      return {
        title: "Screen starting",
        description: "The emulator is booting; the live screen appears once Android is ready.",
      };
    case "stopping":
      return {
        title: "Screen stopping",
        description: "Waiting for the emulator to shut down.",
      };
    case "error":
      return {
        title: "Screen unavailable — start failed",
        description: "Review the instance error, then try starting it again.",
      };
    default:
      return {
        title: "Screen unavailable",
        description: "The daemon is not reporting a capturable screen for this instance.",
      };
  }
}

/**
 * Live, auto-refreshing screen preview with click-to-tap mapping and basic
 * keyboard/text input.
 *
 * The screenshot is in device pixels; taps are scaled by the rendered image
 * size so clicking anywhere on the preview targets the right device coordinate.
 */
export function ScreenPreview({
  name,
  state,
  screenAvailable,
  autoRefreshMs = DEFAULT_REFRESH_MS,
}: ScreenPreviewProps) {
  const [screenSrc, setScreenSrc] = useState<string | null>(null);
  const [frameSize, setFrameSize] = useState<{ width: number; height: number } | null>(null);
  const [capturedAt, setCapturedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);
  const [paused, setPaused] = useState(false);
  const [tapPoint, setTapPoint] = useState<{ left: number; top: number } | null>(null);

  const [keyboardBusy, setKeyboardBusy] = useState(false);
  const [inputError, setInputError] = useState<string | null>(null);
  const [inputStatus, setInputStatus] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [coordinateX, setCoordinateX] = useState("");
  const [coordinateY, setCoordinateY] = useState("");

  const mounted = useRef(true);
  const capturingRef = useRef(false);
  const sourceRef = useRef<ImageSource | null>(null);

  const running = state === "running";
  const canCapture = running && screenAvailable !== false;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      sourceRef.current?.revoke();
      sourceRef.current = null;
    };
  }, []);

  // Reset everything when the route points at a different instance.
  useEffect(() => {
    const previous = sourceRef.current;
    sourceRef.current = null;
    previous?.revoke();
    setScreenSrc(null);
    setFrameSize(null);
    setCapturedAt(null);
    setError(null);
    setPaused(false);
    setTapPoint(null);
    setInputError(null);
    setInputStatus(null);
    setText("");
  }, [name]);

  const capture = useCallback(async (): Promise<void> => {
    if (capturingRef.current) return;
    capturingRef.current = true;
    setCapturing(true);
    try {
      const blob = await api.getScreenshot(name);
      const source = await createImageSource(blob);
      if (!mounted.current) {
        source.revoke();
        return;
      }
      const previous = sourceRef.current;
      sourceRef.current = source;
      setScreenSrc(source.src);
      setError(null);
      setCapturedAt(Date.now());
      previous?.revoke();
    } catch (caught) {
      if (!mounted.current) return;
      setError(screenshotErrorMessage(caught));
    } finally {
      capturingRef.current = false;
      if (mounted.current) setCapturing(false);
    }
  }, [name]);

  useEffect(() => {
    if (!canCapture || paused) return;
    void capture();
    const interval = Math.min(5_000, Math.max(2_000, autoRefreshMs));
    const timer = window.setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      void capture();
    }, interval);
    return () => window.clearInterval(timer);
  }, [canCapture, paused, capture, autoRefreshMs]);

  const sendInput = useCallback(
    async (payload: InputRequest, confirmation: string): Promise<void> => {
      setInputError(null);
      setInputStatus(null);
      setKeyboardBusy(true);
      try {
        await api.sendInput(name, payload);
        if (!mounted.current) return;
        setInputStatus(confirmation);
      } catch (caught) {
        if (!mounted.current) return;
        setInputError(inputErrorMessage(caught));
      } finally {
        if (mounted.current) setKeyboardBusy(false);
      }
    },
    [name],
  );

  const handleScreenClick = (event: MouseEvent<HTMLImageElement>): void => {
    const image = event.currentTarget;
    const rect = image.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const naturalWidth = image.naturalWidth || rect.width;
    const naturalHeight = image.naturalHeight || rect.height;
    const x = Math.round(((event.clientX - rect.left) / rect.width) * naturalWidth);
    const y = Math.round(((event.clientY - rect.top) / rect.height) * naturalHeight);
    setTapPoint({ left: event.clientX - rect.left, top: event.clientY - rect.top });
    setFrameSize({ width: naturalWidth, height: naturalHeight });
    void sendInput({ action: "tap", x, y }, `Tap sent at ${x}, ${y}`);
  };

  const submitText = (): void => {
    const value = text.trim();
    if (!value) return;
    void sendInput({ action: "text", text: value }, `Typed ${value.length} characters`);
    setText("");
  };

  const submitCoordinates = (): void => {
    const x = Number.parseInt(coordinateX, 10);
    const y = Number.parseInt(coordinateY, 10);
    if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0) {
      setInputError("Tap coordinates must be non-negative integers (device pixels).");
      return;
    }
    void sendInput({ action: "tap", x, y }, `Tap sent at ${x}, ${y}`);
  };

  const placeholder = placeholderFor(state);
  const refreshSeconds = Math.round(Math.min(5_000, Math.max(2_000, autoRefreshMs)) / 1000);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", flexWrap: "wrap" }}>
        <StatusPill
          status="running"
          label={running ? (paused ? "Paused" : "Live") : "Idle"}
          tone={running ? (paused ? "neutral" : "success") : "neutral"}
          pulse={running && !paused}
        />
        <span className="muted small">
          {running
            ? paused
              ? "Live view paused"
              : `Auto-refreshing every ${refreshSeconds} s`
            : "Live view inactive"}
          {capturedAt !== null ? ` · captured ${new Date(capturedAt).toLocaleTimeString()}` : ""}
          {frameSize ? ` · ${frameSize.width}×${frameSize.height}` : ""}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          <IconButton
            label={paused ? "Resume live screen" : "Pause live screen"}
            icon={paused ? "play" : "stop"}
            size="sm"
            disabled={!running}
            onClick={() => setPaused((value) => !value)}
          />
          <IconButton
            label="Capture screenshot now"
            icon="refresh"
            size="sm"
            loading={capturing}
            disabled={!canCapture}
            onClick={() => void capture()}
          />
        </div>
      </div>

      {error ? (
        <InlineNotice
          tone="warning"
          title="Could not refresh the screen"
          action={
            <Button size="sm" variant="secondary" icon="refresh" onClick={() => void capture()}>
              Retry
            </Button>
          }
        >
          <p>{error}</p>
        </InlineNotice>
      ) : null}

      {screenAvailable === false && running ? (
        <InlineNotice tone="info">
          <p>The daemon reports that screen capture is not yet available for this instance.</p>
        </InlineNotice>
      ) : null}

      {!running ? (
        <EmptyState compact icon="monitor" title={placeholder.title} description={placeholder.description} />
      ) : (
        <div
          style={{
            position: "relative",
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            minHeight: 220,
            maxHeight: "min(62vh, 640px)",
            overflow: "auto",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-md)",
            background: "var(--surface-sunken)",
          }}
        >
          {screenSrc ? (
            <img
              src={screenSrc}
              alt={`Live screen of ${name}`}
              draggable={false}
              onClick={handleScreenClick}
              onLoad={(event) =>
                setFrameSize({
                  width: event.currentTarget.naturalWidth,
                  height: event.currentTarget.naturalHeight,
                })
              }
              style={{
                display: "block",
                maxWidth: "100%",
                maxHeight: "min(62vh, 640px)",
                width: "auto",
                height: "auto",
                objectFit: "contain",
                cursor: "crosshair",
              }}
            />
          ) : (
            <div style={{ display: "grid", placeItems: "center", padding: "var(--space-8)" }}>
              {capturing ? (
                <Spinner label="Capturing screen" />
              ) : (
                <span className="muted small">No frame captured yet.</span>
              )}
            </div>
          )}
          {tapPoint ? (
            <span
              aria-hidden="true"
              style={{
                position: "absolute",
                left: tapPoint.left,
                top: tapPoint.top,
                width: 16,
                height: 16,
                marginLeft: -8,
                marginTop: -8,
                borderRadius: "50%",
                border: "2px solid var(--accent)",
                background: "var(--accent-soft)",
                pointerEvents: "none",
              }}
            />
          ) : null}
        </div>
      )}

      {running ? (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
          <p className="muted small" style={{ margin: 0 }}>
            Click the screen to send a tap; coordinates are scaled from the preview to device pixels.
          </p>

          <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)" }}>
            {KEYS.map((key) => (
              <Button
                key={key.keycode}
                size="sm"
                variant="secondary"
                disabled={keyboardBusy}
                onClick={() =>
                  void sendInput({ action: "key", keycode: key.keycode }, `${key.label} key sent`)
                }
              >
                {key.label}
              </Button>
            ))}
          </div>

          <form
            onSubmit={(event) => {
              event.preventDefault();
              submitText();
            }}
            style={{ display: "flex", gap: "var(--space-2)" }}
          >
            <Input
              aria-label="Text to type"
              placeholder="Type text on the device…"
              value={text}
              disabled={keyboardBusy}
              onChange={(event) => setText(event.target.value)}
            />
            <Button type="submit" size="md" variant="secondary" disabled={keyboardBusy || text.trim() === ""}>
              Send text
            </Button>
          </form>

          <form
            onSubmit={(event) => {
              event.preventDefault();
              submitCoordinates();
            }}
            style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", flexWrap: "wrap" }}
          >
            <span className="muted small">Tap by coordinates</span>
            <Input
              aria-label="Tap X coordinate"
              placeholder="X"
              type="number"
              min={0}
              inputMode="numeric"
              value={coordinateX}
              disabled={keyboardBusy}
              onChange={(event) => setCoordinateX(event.target.value)}
              style={{ width: 96 }}
            />
            <Input
              aria-label="Tap Y coordinate"
              placeholder="Y"
              type="number"
              min={0}
              inputMode="numeric"
              value={coordinateY}
              disabled={keyboardBusy}
              onChange={(event) => setCoordinateY(event.target.value)}
              style={{ width: 96 }}
            />
            <Button type="submit" size="md" variant="secondary" disabled={keyboardBusy}>
              Tap
            </Button>
          </form>

          {inputError ? (
            <p role="alert" className="ds-field__error" style={{ margin: 0 }}>
              {inputError}
            </p>
          ) : null}
          {inputStatus ? (
            <p role="status" className="muted small" style={{ margin: 0 }}>
              {inputStatus}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
