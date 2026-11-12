/**
 * Ceremony options, held ready before the button is pressed -- and kept fresh.
 *
 * They have to be fetched ahead of the click: navigator.credentials needs the
 * user gesture that started it, and Safari drops that activation across an
 * awaited network request, reporting the loss as NotAllowedError. But options
 * fetched once and then held are a different failure: the server redeems a
 * challenge for five minutes, and a page left open longer than that offers a
 * button that is guaranteed to fail with "expired".
 *
 * So this refreshes well inside that window, again when the tab comes back
 * into view (timers are throttled in background tabs, so the interval alone
 * cannot be trusted), and after every use. A failed fetch is reported rather
 * than swallowed, so a disabled button always comes with a reason.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, primeCsrf } from "./api";

type Options = Record<string, unknown>;

/** Inside the server's five-minute challenge lifetime, with margin for a slow request. */
const REFRESH_MS = 3.5 * 60 * 1000;

/** Older than this when the tab regains focus, and it is refetched before use. */
const STALE_ON_RETURN_MS = 60 * 1000;

export function usePasskeyOptions(url: string, enabled: boolean) {
  const [options, setOptions] = useState<Options | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fetchedAt = useRef(0);
  // Only the newest request may land. Without this a slow response could
  // replace a newer set of options with an older one.
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const mine = ++generation.current;
    try {
      await primeCsrf();
      const fetched = await api.post<Options>(url, {});
      if (mine !== generation.current) return;
      setOptions(fetched);
      setError(null);
      fetchedAt.current = Date.now();
    } catch (err) {
      if (mine !== generation.current) return;
      setOptions(null);
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not reach the panel to prepare a passkey request.",
      );
    }
  }, [url]);

  useEffect(() => {
    if (!enabled) {
      generation.current++;
      setOptions(null);
      setError(null);
      return;
    }

    refresh();
    const timer = window.setInterval(refresh, REFRESH_MS);
    const onVisible = () => {
      if (
        document.visibilityState === "visible" &&
        Date.now() - fetchedAt.current > STALE_ON_RETURN_MS
      ) {
        refresh();
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, refresh]);

  /**
   * Hand the options over for one ceremony, and start fetching the next set.
   *
   * Taken rather than read: a challenge is single-use on the server, so the
   * moment it is in use the button must not offer it again.
   */
  const take = useCallback((): Options | null => {
    const current = options;
    setOptions(null);
    return current;
  }, [options]);

  return { options, error, refresh, take };
}
