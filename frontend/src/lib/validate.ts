/* ---------------------------------------------------------------------------
   Validation that mirrors the server.

   These rules exist in management/common/validators.py, which is where they
   are enforced. Repeating them here is not a second opinion: it is the same
   rule, phrased the same way, applied before the operator commits rather than
   after. Where the wording differs from the server's, the server is right --
   seeing one rule described two ways is worse than a round trip.

   Anything that needs state the browser does not have stays on the server.
   --------------------------------------------------------------------------- */

const CONTROL = /[\x00-\x1f\x7f]/;
const STRUCTURAL = /[;{}#\s]/;
const HOSTNAME =
  /^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const IDENTIFIER = /^[a-zA-Z0-9][a-zA-Z0-9._-]*$/;

/** Null means valid. A string is the message to show under the field. */
export type Invalid = string | null;

export function hostname(value: string): Invalid {
  const name = value.trim();
  if (!name) return "A hostname is required.";
  if (name.includes("://") || name.includes("/"))
    return "Enter the hostname only, without https:// or a path.";
  if (name.length > 253) return "A hostname may be at most 253 characters.";
  if (!HOSTNAME.test(name.toLowerCase()))
    return `'${name}' is not a valid hostname. Use letters, digits, hyphens and dots.`;
  return null;
}

export function matchValue(value: string, isRegex: boolean): Invalid {
  if (!value) return "A match value is required.";
  if (CONTROL.test(value)) return "A match value may not contain control characters.";
  if (isRegex) {
    if (/[;#\s]/.test(value))
      return "A regex may not contain spaces, ';' or '#'. Use '\\s' for whitespace.";
    return null;
  }
  if (!value.startsWith("/")) return `A path must start with '/' — did you mean '/${value}'?`;
  if (STRUCTURAL.test(value))
    return "A path may not contain spaces, ';', '#', '{' or '}'. Use a regex match instead.";
  return null;
}

export function headerName(value: string): Invalid {
  if (!value) return "A header name is required.";
  if (!HEADER_NAME.test(value))
    return `'${value}' is not a valid header name. Use letters, digits and hyphens, like 'X-Route'.`;
  return null;
}

export function identifier(value: string, label = "name"): Invalid {
  if (!value) return `A ${label} is required.`;
  if (!IDENTIFIER.test(value))
    return `A ${label} may contain only letters, digits, dots, hyphens and underscores, and must start with a letter or digit.`;
  return null;
}

export function configPath(value: string, label = "path"): Invalid {
  if (!value) return null;
  if (STRUCTURAL.test(value))
    return `A ${label} may not contain spaces, ';', '#', '{' or '}'.`;
  if (!value.startsWith("/")) return `A ${label} must be absolute, starting with '/'.`;
  if (value.split("/").includes("..")) return `A ${label} may not contain '..'.`;
  return null;
}

export function range(
  value: number | string,
  { low, high, label, unit = "" }: { low: number; high: number; label: string; unit?: string },
): Invalid {
  const n = typeof value === "string" ? Number(value) : value;
  if (value === "" || value === null || Number.isNaN(n)) return `A ${label} is required.`;
  const suffix = unit ? ` ${unit}` : "";
  if (n < low || n > high)
    return `${label[0].toUpperCase()}${label.slice(1)} must be between ${low}${suffix} and ${high}${suffix}; got ${n}${suffix}.`;
  return null;
}

/* ---------------------------------------------------------------------------
   Cross-field rules. These are the ones an operator cannot see coming, because
   each value is reasonable and only the combination is wrong.
   --------------------------------------------------------------------------- */

/**
 * HTTP on 443 opens a cleartext socket to a TLS port; HTTPS on 80 does the
 * reverse. Either way the backend answers with something that looks like a
 * routing fault, which is where the time goes.
 */
export function schemePort(scheme: string, port: number | string): Invalid {
  const n = typeof port === "string" ? Number(port) : port;
  if (scheme === "http" && n === 443)
    return "Port 443 is a TLS port but the protocol is HTTP. Switch the protocol to HTTPS, or change the port.";
  if (scheme === "https" && n === 80)
    return "Port 80 is a cleartext port but the protocol is HTTPS. Switch the protocol to HTTP, or change the port.";
  return null;
}

/** A probe that outlives its own interval queues up behind the next one. */
export function healthTiming(timeout: number | string, interval: number | string): Invalid {
  const t = Number(timeout);
  const i = Number(interval);
  if (Number.isNaN(t) || Number.isNaN(i)) return null;
  if (t >= i)
    return `A ${t}s timeout does not fit in a ${i}s interval — probes would overlap. Shorten the timeout or lengthen the interval.`;
  return null;
}

/** Collapse a set of field results into a submit guard. */
export function firstProblem(results: Record<string, Invalid>): Invalid {
  for (const key of Object.keys(results)) {
    if (results[key]) return results[key];
  }
  return null;
}
