/**
 * Who is signed in, available anywhere without threading it through props.
 *
 * This is presentation only. Every endpoint checks the role itself, and the
 * panel would be just as safe showing every button to everyone — it would
 * only be ruder, because each one would return "Your role does not allow
 * that" after the person had already decided to use it.
 *
 * So the rule for using `can()` is: hide a control the role can never work,
 * never use it as the reason something is safe.
 */
import { ReactNode, createContext, useContext } from "react";
import { User } from "./types";

const RANK: Record<string, number> = { viewer: 0, operator: 1, admin: 2 };

const SessionContext = createContext<User | null>(null);

export function SessionProvider({
  user,
  children,
}: {
  user: User;
  children: ReactNode;
}) {
  return <SessionContext.Provider value={user}>{children}</SessionContext.Provider>;
}

export function useSession(): User | null {
  return useContext(SessionContext);
}

/**
 * Whether the signed-in account holds `required` or better.
 *
 * An account with no role holds nothing, which is what the server decided
 * too. Outside a provider this answers false rather than throwing: a stray
 * call should hide a button, not blank the page.
 */
export function useCan(required: "viewer" | "operator" | "admin"): boolean {
  const user = useContext(SessionContext);
  const role = user?.role;
  if (!role || !(role in RANK)) return false;
  return RANK[role] >= RANK[required];
}
