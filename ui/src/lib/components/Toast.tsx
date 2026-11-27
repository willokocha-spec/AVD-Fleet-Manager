import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { cx } from "../utils";
import { Icon, type IconName } from "./Icon";

export type ToastTone = "info" | "success" | "warning" | "danger";

export interface ToastOptions {
  title: string;
  description?: ReactNode;
  tone?: ToastTone;
  /** Auto-dismiss delay in ms; `0` keeps the toast until dismissed. */
  duration?: number;
}

interface ToastItem extends ToastOptions {
  id: string;
  tone: ToastTone;
  duration: number;
}

export interface ToastApi {
  toast: (options: ToastOptions | string) => string;
  success: (title: string, description?: ReactNode) => string;
  error: (title: string, description?: ReactNode) => string;
  warning: (title: string, description?: ReactNode) => string;
  info: (title: string, description?: ReactNode) => string;
  dismiss: (id: string) => void;
  clear: () => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const TONE_ICON: Record<ToastTone, IconName> = {
  info: "info",
  success: "check-circle",
  warning: "alert-triangle",
  danger: "alert-circle",
};

const MAX_TOASTS = 5;
const DEFAULT_DURATION = 5000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const timers = useRef(new Map<string, number>());
  const counter = useRef(0);

  const dismiss = useCallback((id: string) => {
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
    setToasts((current) => current.filter((item) => item.id !== id));
  }, []);

  const clear = useCallback(() => {
    for (const timer of timers.current.values()) window.clearTimeout(timer);
    timers.current.clear();
    setToasts([]);
  }, []);

  useEffect(() => {
    const active = timers.current;
    return () => {
      for (const timer of active.values()) window.clearTimeout(timer);
      active.clear();
    };
  }, []);

  const toast = useCallback(
    (options: ToastOptions | string): string => {
      const normalized: ToastOptions = typeof options === "string" ? { title: options } : options;
      counter.current += 1;
      const id = `toast-${counter.current}`;
      const item: ToastItem = {
        id,
        tone: normalized.tone ?? "info",
        title: normalized.title,
        description: normalized.description,
        duration: normalized.duration ?? DEFAULT_DURATION,
      };
      setToasts((current) => [...current, item].slice(-MAX_TOASTS));
      if (item.duration > 0) {
        const timer = window.setTimeout(() => dismiss(id), item.duration);
        timers.current.set(id, timer);
      }
      return id;
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      toast,
      dismiss,
      clear,
      success: (title, description) => toast({ title, description, tone: "success" }),
      error: (title, description) => toast({ title, description, tone: "danger", duration: 8000 }),
      warning: (title, description) => toast({ title, description, tone: "warning", duration: 7000 }),
      info: (title, description) => toast({ title, description, tone: "info" }),
    }),
    [clear, dismiss, toast],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="ds-toasts" role="region" aria-label="Notifications">
        {toasts.map((item) => (
          <div
            key={item.id}
            className={cx("ds-toast", `ds-toast--${item.tone}`)}
            role={item.tone === "danger" ? "alert" : "status"}
            data-testid="toast"
          >
            <span className="ds-toast__icon" aria-hidden="true">
              <Icon name={TONE_ICON[item.tone]} size={18} />
            </span>
            <div className="ds-toast__body">
              <p className="ds-toast__title">{item.title}</p>
              {item.description ? <div className="ds-toast__description">{item.description}</div> : null}
            </div>
            <button
              type="button"
              className="ds-toast__close"
              onClick={() => dismiss(item.id)}
              aria-label={`Dismiss notification: ${item.title}`}
            >
              <Icon name="close" size={15} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const context = useContext(ToastContext);
  if (!context) throw new Error("useToast must be used inside <ToastProvider>");
  return context;
}
