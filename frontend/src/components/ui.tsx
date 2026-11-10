import { ReactNode, createContext, useCallback, useContext, useState } from "react";
import { useEscape } from "../lib/hooks";

/* ---------------------------------------------------------------------------
   Panel
   --------------------------------------------------------------------------- */

export function Panel({
  title,
  hint,
  actions,
  children,
  flush,
}: {
  title: string;
  hint?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="panel">
      <header className="panel__head">
        <div>
          <h2 className="panel__title">{title}</h2>
          {hint && <div className="panel__hint">{hint}</div>}
        </div>
        {actions}
      </header>
      <div className={flush ? "panel__body panel__body--flush" : "panel__body"}>
        {children}
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------------------
   Row — the status rail on the left carries state, so tags do not have to.
   --------------------------------------------------------------------------- */

export type RowState = "healthy" | "degraded" | "down" | "idle" | "active";

export function Row({
  state = "idle",
  columns,
  children,
}: {
  state?: RowState;
  columns: string;
  children: ReactNode;
}) {
  return (
    <div className={`row row--${state}`} style={{ gridTemplateColumns: columns }}>
      {children}
    </div>
  );
}

/* ---------------------------------------------------------------------------
   Tags
   --------------------------------------------------------------------------- */

export function Tag({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "hit" | "warn" | "down" | "signal" | "cool";
  children: ReactNode;
}) {
  return <span className={tone === "neutral" ? "tag" : `tag tag--${tone}`}>{children}</span>;
}

/* ---------------------------------------------------------------------------
   Empty states — an invitation to act, not an apology.
   --------------------------------------------------------------------------- */

export function Empty({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty__title">{title}</div>
      <p className="empty__body">{body}</p>
      {action}
    </div>
  );
}

export function Notice({
  tone = "info",
  children,
}: {
  tone?: "info" | "warn" | "error";
  children: ReactNode;
}) {
  return <div className={tone === "info" ? "notice" : `notice notice--${tone}`}>{children}</div>;
}

/* ---------------------------------------------------------------------------
   Modal
   --------------------------------------------------------------------------- */

export function Modal({
  title,
  subtitle,
  onClose,
  footer,
  wide,
  children,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  footer?: ReactNode;
  wide?: boolean;
  children: ReactNode;
}) {
  useEscape(onClose);

  return (
    <div
      className="overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className={wide ? "modal modal--wide" : "modal"}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header className="modal__head">
          <div>
            <h2 className="modal__title">{title}</h2>
            {subtitle && <p className="modal__sub">{subtitle}</p>}
          </div>
          <button className="btn btn--ghost btn--small" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        <div className="modal__body">{children}</div>
        {footer && <footer className="modal__foot">{footer}</footer>}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------------------
   Form fields
   --------------------------------------------------------------------------- */

export function Field({
  label,
  help,
  error,
  children,
}: {
  label: string;
  help?: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      {children}
      {error ? (
        <span className="field__error">{error}</span>
      ) : (
        help && <span className="field__help">{help}</span>
      )}
    </label>
  );
}

export function Check({
  label,
  help,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  help?: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>
        <span className="check__text">{label}</span>
        {help && <span className="check__help">{help}</span>}
      </span>
    </label>
  );
}

/* ---------------------------------------------------------------------------
   Toasts — an action's name stays the same from button to confirmation.
   --------------------------------------------------------------------------- */

interface Toast {
  id: number;
  message: string;
  tone: "ok" | "error";
}

const ToastContext = createContext<(message: string, tone?: "ok" | "error") => void>(
  () => undefined,
);

export function useToast() {
  return useContext(ToastContext);
}

export function ToastHost({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((message: string, tone: "ok" | "error" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current, { id, message, tone }]);
    window.setTimeout(
      () => setToasts((current) => current.filter((t) => t.id !== id)),
      tone === "error" ? 8000 : 4500,
    );
  }, []);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={toast.tone === "error" ? "toast toast--error" : "toast"}
          >
            {toast.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/* ---------------------------------------------------------------------------
   Progressive disclosure

   A form that shows every setting at once gives them all the same weight, and
   an operator who does not already know the system cannot tell which three
   matter. These hide the rest -- but never silently: the summary counts the
   settings that differ from their defaults, so a collapsed section always
   declares that something inside it has been changed.
   --------------------------------------------------------------------------- */

export function Advanced({
  label,
  changed = 0,
  children,
  defaultOpen = false,
}: {
  label: string;
  changed?: number;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen || changed > 0);

  return (
    <div className={`advanced${open ? " advanced--open" : ""}`}>
      <button
        type="button"
        className="advanced__toggle"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="advanced__chevron" aria-hidden="true" />
        <span className="advanced__label">{label}</span>
        {changed > 0 && (
          <span className="advanced__count">
            {changed} changed
          </span>
        )}
      </button>
      {open && <div className="advanced__body">{children}</div>}
    </div>
  );
}

/* ---------------------------------------------------------------------------
   Outcome

   What the form will actually do, in a sentence, updating as it is filled in.
   The operator confirms a result rather than auditing a list of fields -- which
   is the difference between understanding a route and merely having typed one.
   --------------------------------------------------------------------------- */

export function Outcome({ children }: { children: ReactNode }) {
  return (
    <p className="outcome" aria-live="polite">
      {children}
    </p>
  );
}

/* ---------------------------------------------------------------------------
   A text input that reports its own problem.

   Validation runs on blur rather than on every keystroke: telling someone the
   hostname is invalid while they are still typing the first letter of it is
   noise, not help. Once a field has been corrected the message clears
   immediately, so the fix is acknowledged without waiting for another blur.
   --------------------------------------------------------------------------- */

export function Validated({
  label,
  help,
  value,
  onChange,
  check,
  placeholder,
  type = "text",
  mono = false,
  disabled,
}: {
  label: string;
  help?: string;
  value: string;
  onChange: (value: string) => void;
  check?: (value: string) => string | null;
  placeholder?: string;
  type?: string;
  mono?: boolean;
  disabled?: boolean;
}) {
  const [touched, setTouched] = useState(false);
  const problem = check ? check(value) : null;
  const shown = touched ? problem : null;

  return (
    <Field label={label} help={help} error={shown ?? undefined}>
      <input
        type={type}
        className={`input${mono ? " input--mono" : ""}${shown ? " input--bad" : ""}`}
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        aria-invalid={shown ? true : undefined}
        onBlur={() => setTouched(true)}
        onChange={(event) => {
          onChange(event.target.value);
          // Clear a shown message the moment it stops being true.
          if (touched && check && !check(event.target.value)) setTouched(false);
        }}
      />
    </Field>
  );
}
