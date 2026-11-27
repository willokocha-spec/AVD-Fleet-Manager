import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";

import type { ApiError } from "./api";

export interface AsyncState<T> {
  data: T | null;
  error: ApiError | Error | null;
  loading: boolean;
  /** Re-run the loader (keeps the previous data visible until it resolves). */
  reload: () => void;
  /** Replace data locally after a mutation. */
  setData: (updater: T | ((current: T | null) => T | null)) => void;
}

/**
 * Tiny data-loading hook used by pages.
 *
 * ```ts
 * const instances = useAsync((signal) => api.listInstances(), []);
 * ```
 * The loader receives an AbortSignal; requests still in flight when the
 * component unmounts (or deps change) are ignored.
 */
export function useAsync<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList = [],
  options: { immediate?: boolean } = {},
): AsyncState<T> {
  const { immediate = true } = options;
  const [data, setDataState] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [loading, setLoading] = useState<boolean>(immediate);
  const [nonce, setNonce] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    if (!immediate) return;
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError(null);
    loaderRef
      .current(controller.signal)
      .then((result) => {
        if (!active) return;
        setDataState(result);
        setLoading(false);
      })
      .catch((caught: unknown) => {
        if (!active) return;
        if (caught instanceof Error && caught.name === "AbortError") return;
        setError(caught instanceof Error ? caught : new Error(String(caught)));
        setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deps are the caller's contract
  }, [nonce, immediate, ...deps]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  const setData = useCallback((updater: T | ((current: T | null) => T | null)) => {
    setDataState((current) =>
      typeof updater === "function" ? (updater as (value: T | null) => T | null)(current) : updater,
    );
  }, []);

  return { data, error, loading, reload, setData };
}

/** Debounce a rapidly changing value (search boxes). */
export function useDebouncedValue<T>(value: T, delayMs = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [delayMs, value]);
  return debounced;
}
