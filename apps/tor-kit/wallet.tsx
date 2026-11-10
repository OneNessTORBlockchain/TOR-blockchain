/** Wallet: node account login, balance, send, receive QR, key import/export. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { TorError, api, readSession, writeSession } from "./api";
import type { Session } from "./api";
import { shortAddress, tor } from "./brand";
import { useToast } from "./ui";

type Profile = {
  account: { username?: string; role?: string } | null;
  wallet: { name: string; address: string; balance_tor?: string; tokens?: Record<string, number> };
};

type Ctx = {
  session: Session | null;
  profile: Profile | null;
  busy: boolean;
  oneTime: { privateKey: string; mnemonic: string } | null;
  login: (username: string, password: string) => Promise<void>;
  signup: (username: string, password: string) => Promise<void>;
  logout: () => void;
  refresh: () => Promise<void>;
  clearOneTime: () => void;
};

const WalletContext = createContext<Ctx>({
  session: null, profile: null, busy: false, oneTime: null,
  login: async () => undefined, signup: async () => undefined,
  logout: () => undefined, refresh: async () => undefined,
  clearOneTime: () => undefined,
});

export function WalletProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(() => readSession());
  const [profile, setProfile] = useState<Profile | null>(null);
  const [busy, setBusy] = useState(false);
  const [oneTime, setOneTime] = useState<Ctx["oneTime"]>(null);
  const toast = useToast();

  const refresh = useCallback(async () => {
    if (!readSession()) return setProfile(null);
    try {
      setProfile(await api.get<Profile>("/api/auth/me"));
    } catch {
      setProfile(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const open = useCallback(
    async (path: "/api/auth/login" | "/api/auth/signup", username: string, password: string) => {
      setBusy(true);
      try {
        const data = await api.post<{
          wallet: { name: string; address: string };
          session: { token: string; header: string };
          one_time?: { private_key_base58: string; mnemonic: string };
        }>(path, { username, password });
        const next: Session = {
          wallet: data.wallet.name, address: data.wallet.address,
          token: data.session.token, header: data.session.header,
        };
        writeSession(next);
        setSession(next);
        await refresh();
        if (data.one_time) {
          setOneTime({ privateKey: data.one_time.private_key_base58,
                       mnemonic: data.one_time.mnemonic });
        }
        toast(path.endsWith("signup") ? "wallet created" : `signed in as ${data.wallet.name}`);
      } finally {
        setBusy(false);
      }
    },
    [refresh, toast],
  );

  const value = useMemo<Ctx>(() => ({
    session, profile, busy, oneTime,
    login: (u, p) => open("/api/auth/login", u, p),
    signup: (u, p) => open("/api/auth/signup", u, p),
    logout: () => {
      api.post("/api/auth/logout").catch(() => undefined);
      writeSession(null);
      setSession(null);
      setProfile(null);
      setOneTime(null);
    },
    refresh,
    clearOneTime: () => setOneTime(null),
  }), [session, profile, busy, oneTime, open, refresh]);

  return <WalletContext.Provider value={value}>{children}</WalletContext.Provider>;
}

export function useWallet() {
  return useContext(WalletContext);
}

export function WalletPill({ onOpen }: { onOpen?: () => void }) {
  const { session, profile, logout } = useWallet();
  if (!session) return <button className="primary small" onClick={onOpen}>Sign in</button>;
  return (
    <div className="row" style={{ gap: 8 }}>
      <span className="pill on"><span className="dot" />{shortAddress(profile?.wallet?.address || session.address)}</span>
      <button className="ghost small" onClick={logout}>Sign out</button>
    </div>
  );
}

export function SignInPanel({ onDone }: { onDone?: () => void }) {
  const { login, signup, busy, oneTime, clearOneTime } = useWallet();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  if (oneTime) {
    return (
      <div className="panel stack">
        <h2>Save your private key</h2>
        <p className="muted tiny">Shown once. Anyone holding it controls the wallet.</p>
        <textarea readOnly rows={2} value={oneTime.privateKey} />
        <textarea readOnly rows={2} value={oneTime.mnemonic} />
        <button className="primary" onClick={() => { clearOneTime(); onDone?.(); }}>
          I stored it — continue
        </button>
      </div>
    );
  }

  return (
    <form
      className="panel stack"
      onSubmit={async (event) => {
        event.preventDefault();
        setError("");
        try {
          if (mode === "login") await login(username.trim(), password);
          else await signup(username.trim(), password);
          onDone?.();
        } catch (problem) {
          setError(problem instanceof TorError ? problem.message : (problem as Error).message);
        }
      }}
    >
      <div className="tabs">
        <button type="button" className={mode === "login" ? "on" : ""} onClick={() => setMode("login")}>Sign in</button>
        <button type="button" className={mode === "signup" ? "on" : ""} onClick={() => setMode("signup")}>New wallet</button>
      </div>
      <div className="field">
        <label>Username</label>
        <input value={username} onChange={(event) => setUsername(event.target.value)} required />
      </div>
      <div className="field">
        <label>Password</label>
        <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required />
      </div>
      {error ? <div className="tiny bad">{error}</div> : null}
      <button className="primary" disabled={busy}>
        {busy ? "working…" : mode === "login" ? "Open my wallet" : "Create wallet + key"}
      </button>
    </form>
  );
}


/** Balance, receive QR, send, import/export keys, recent history. */
export function WalletPanel() {
  const { session, profile, refresh } = useWallet();
  const [tab, setTab] = useState<"receive" | "send" | "keys">("receive");
  const [receive, setReceive] = useState<{ address: string; qr: { address: { svg: string } } } | null>(null);
  const [history, setHistory] = useState<{ records: Record<string, unknown>[] } | null>(null);
  const [to, setTo] = useState("");
  const [amountValue, setAmountValue] = useState("1");
  const [password, setPassword] = useState("");
  const [exported, setExported] = useState("");
  const [imported, setImported] = useState("");
  const [message, setMessage] = useState("");
  const toast = useToast();
  const walletName = session?.wallet || "";

  useEffect(() => {
    if (!walletName) return;
    api.get<{ address: string; qr: { address: { svg: string } } }>(`/api/wallets/${walletName}/receive`)
      .then(setReceive).catch(() => undefined);
    api.get<{ records: Record<string, unknown>[] }>(`/api/wallets/${walletName}/history?days=30`)
      .then(setHistory).catch(() => undefined);
  }, [walletName]);

  if (!session) return <SignInPanel />;
  const balance = profile?.wallet?.balance_tor ?? "0";
  const tokens = profile?.wallet?.tokens || {};

  return (
    <div className="stack">
      <div className="grid cols-3">
        <div className="stat"><span>Wallet</span><b className="mono tiny">{walletName}</b><em>{shortAddress(session.address)}</em></div>
        <div className="stat"><span>TOR balance</span><b>{tor(Number(balance))}</b><em>live from the ledger</em></div>
        <div className="stat"><span>Token holdings</span><b>{Object.keys(tokens).length}</b>
          <em>{Object.entries(tokens).slice(0, 2).map(([k, v]) => `${k} ${v}`).join(" · ") || "none yet"}</em></div>
      </div>

      <div className="panel stack">
        <div className="tabs">
          {(["receive", "send", "keys"] as const).map((item) => (
            <button key={item} className={tab === item ? "on" : ""} onClick={() => setTab(item)}>
              {item === "receive" ? "Receive" : item === "send" ? "Send" : "Import / export"}
            </button>
          ))}
        </div>

        {tab === "receive" ? (
          <div className="grid cols-2">
            <div className="stack">
              <div className="field"><label>Address</label>
                <input readOnly value={receive?.address || session.address} />
              </div>
              <p className="muted tiny">Balances are public; spending needs this session.</p>
              {history?.records?.length ? (
                <table>
                  <tbody>
                    {history.records.slice(0, 6).map((row, index) => (
                      <tr key={index}>
                        <td className="tiny mono">{String(row.kind || "")}</td>
                        <td className="tiny">{String(row.token || "TOR")}</td>
                        <td className="tiny num">{String(row.amount_tor || "")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
            </div>
            <div className="panel tight" style={{ display: "grid", placeItems: "center" }}>
              {receive?.qr?.address?.svg
                ? <img src={receive.qr.address.svg} alt="receive QR" width={190} height={190} />
                : <div className="skeleton" style={{ width: 190, height: 190 }} />}
            </div>
          </div>
        ) : null}

        {tab === "send" ? (
          <form className="stack" onSubmit={async (event) => {
            event.preventDefault();
            setMessage("");
            try {
              await api.post(`/api/wallets/${walletName}/send`, { to: to.trim(), amount: Number(amountValue) });
              toast(`sent ${amountValue} TOR`);
              await refresh();
            } catch (problem) {
              const text = problem instanceof TorError ? problem.message : "send failed";
              setMessage(text);
              toast(text, true);
            }
          }}>
            <div className="field"><label>Recipient</label>
              <input value={to} onChange={(event) => setTo(event.target.value)} placeholder="TOR1…onion" required />
            </div>
            <div className="field"><label>Amount (TOR)</label>
              <input value={amountValue} onChange={(event) => setAmountValue(event.target.value)} inputMode="decimal" required />
            </div>
            {message ? <div className="tiny bad">{message}</div> : null}
            <button className="primary">Send TOR</button>
          </form>
        ) : null}

        {tab === "keys" ? (
          <div className="grid cols-2">
            <div className="stack">
              <div className="field"><label>Account password</label>
                <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
              </div>
              <button onClick={async () => {
                try {
                  const data = await api.post<{ secret_key_base58?: string; mnemonic?: string }>(
                    `/api/auth/wallets/${walletName}/export`, { password });
                  setExported(data.secret_key_base58 || data.mnemonic || "");
                  toast("key released — store it offline");
                } catch (problem) {
                  toast(problem instanceof TorError ? problem.message : "export failed", true);
                }
              }}>Export private key</button>
              {exported ? <textarea readOnly rows={3} value={exported} /> : null}
            </div>
            <div className="stack">
              <div className="field"><label>Import a private key</label>
                <textarea rows={3} value={imported} onChange={(event) => setImported(event.target.value)} />
              </div>
              <button disabled={!imported.trim() || !password} onClick={async () => {
                try {
                  await api.post("/api/auth/wallets/import", { password, secret_key_base58: imported.trim() });
                  setImported("");
                  toast("wallet imported");
                  await refresh();
                } catch (problem) {
                  toast(problem instanceof TorError ? problem.message : "import failed", true);
                }
              }}>Import into my account</button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

