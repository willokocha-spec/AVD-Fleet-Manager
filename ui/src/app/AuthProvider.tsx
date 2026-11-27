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

import { ApiError, api, clearStoredToken, getStoredToken, onUnauthorized, setStoredToken } from "@/lib/api";
import { useToast } from "@/lib/components";

export type AuthStatus = "checking" | "authenticated" | "anonymous";

interface AuthContextValue {
  status: AuthStatus;
  token: string | null;
  /** Validate and store a token; rejects with `ApiError` on failure. */
  login: (token: string) => Promise<void>;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * Bearer-token auth. On boot, an existing `localStorage["avdfleet.token"]` is
 * verified against `GET /api/v1/auth/check`. A 401 from any request drops the
 * session and returns to the connect screen.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [status, setStatus] = useState<AuthStatus>(() => (getStoredToken() ? "checking" : "anonymous"));
  const [token, setToken] = useState<string | null>(() => getStoredToken());
  const statusRef = useRef<AuthStatus>(status);
  statusRef.current = status;

  const verify = useCallback(async (): Promise<void> => {
    setStatus("checking");
    try {
      await api.checkAuth();
      setStatus("authenticated");
    } catch (error) {
      clearStoredToken();
      setToken(null);
      setStatus("anonymous");
      throw error;
    }
  }, []);

  useEffect(() => {
    const stored = getStoredToken();
    if (!stored) {
      setStatus("anonymous");
      return;
    }
    setToken(stored);
    void verify().catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 401) {
        toast.warning("Token rejected", "The stored bearer token is not accepted by this daemon.");
      }
      // Network failures keep the token so the connect screen can retry with it.
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once on mount
  }, []);

  useEffect(() => {
    const unsubscribe = onUnauthorized(() => {
      clearStoredToken();
      setToken(null);
      if (statusRef.current === "authenticated") {
        setStatus("anonymous");
        toast.warning("Session expired", "The daemon rejected the token. Connect again to continue.");
      } else {
        setStatus("anonymous");
      }
    });
    return unsubscribe;
  }, [toast]);

  const login = useCallback(
    async (candidate: string): Promise<void> => {
      const trimmed = candidate.trim();
      if (!trimmed) throw new ApiError("Enter the bearer token from the daemon's data directory.", { kind: "http", status: 0, code: "invalid_token" });
      setStoredToken(trimmed);
      setToken(trimmed);
      await verify();
      toast.success("Connected", "Authenticated with the AVD Fleet daemon.");
    },
    [toast, verify],
  );

  const signOut = useCallback(() => {
    clearStoredToken();
    setToken(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo<AuthContextValue>(() => ({ status, token, login, signOut }), [login, signOut, status, token]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside <AuthProvider>");
  return context;
}
