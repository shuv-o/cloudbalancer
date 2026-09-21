import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api";

interface Resource<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  /** True only on the first load, so polling never flashes a spinner. */
  initialLoading: boolean;
  reload: () => Promise<void>;
}

/**
 * Fetch a path, optionally re-fetching on an interval.
 *
 * Polling deliberately does not clear the previous data: an operations screen
 * that blanks every few seconds is unreadable. New numbers replace old ones in
 * place, and a failed poll leaves the last good values on screen rather than
 * wiping the page because one request timed out.
 */
export function useResource<T>(path: string | null, intervalMs = 0): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const [initialLoading, setInitialLoading] = useState(Boolean(path));
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      const result = await api.get<T>(path);
      if (!mounted.current) return;
      setData(result);
      setError(null);
    } catch (err) {
      if (!mounted.current) return;
      setError(err as ApiError);
    } finally {
      if (mounted.current) {
        setLoading(false);
        setInitialLoading(false);
      }
    }
  }, [path]);

  useEffect(() => {
    void load();
    if (!intervalMs || !path) return;

    let timer: number | undefined;
    const tick = () => {
      // Polling pauses on a hidden tab. A dashboard left open overnight should
      // not spend the night asking a question nobody is reading the answer to.
      if (!document.hidden) void load();
      timer = window.setTimeout(tick, intervalMs);
    };
    timer = window.setTimeout(tick, intervalMs);
    return () => window.clearTimeout(timer);
  }, [load, intervalMs, path]);

  return { data, error, loading, initialLoading, reload: load };
}

/** Run a write and track its in-flight state, without a global store. */
export function useAction<Args extends unknown[], R>(
  fn: (...args: Args) => Promise<R>,
): [(...args: Args) => Promise<R | undefined>, { busy: boolean; error: ApiError | null }] {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const run = useCallback(
    async (...args: Args) => {
      setBusy(true);
      setError(null);
      try {
        return await fn(...args);
      } catch (err) {
        setError(err as ApiError);
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [fn],
  );

  return [run, { busy, error }];
}

/** Close on Escape — used by every overlay. */
export function useEscape(onEscape: () => void, active = true) {
  useEffect(() => {
    if (!active) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onEscape();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onEscape, active]);
}

/** Light or dark, remembered across visits. */
export function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState<string>(
    () => localStorage.getItem("panel-theme") ?? "dark",
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("panel-theme", theme);
  }, [theme]);

  const toggle = useCallback(
    () => setTheme((t) => (t === "dark" ? "light" : "dark")),
    [],
  );
  return [theme, toggle];
}
