/* OneNess TOR — shared page runtime: real API calls, live log, 3D tilt, toasts. */
const OT = (() => {
  const SESSION_KEY = "oneness.adminSession";
  const WALLET_KEY = "oneness.walletSession";
  const WALLET_NAME_KEY = "oneness.walletName";
  /* Exactly two pages belong to the node operator: /admin (admin wallet,
     settings, chain maintenance) and /ollama (runtime + model routing).
     Every other page is public and works with the visitor's own account
     wallet (username + password → x-wallet-session). No page ever asks for
     the chain admin token: it only exists server-side, bound to the admin
     account at registration. */
  const ADMIN_PAGES = ["admin", "ollama"];
  const fmt = new Intl.NumberFormat("en-US");

  function read(key) {
    try { return localStorage.getItem(key) || ""; } catch (_) { return ""; }
  }

  function write(key, value) {
    try {
      const text = String(value || "").trim();
      if (text) localStorage.setItem(key, text);
      else localStorage.removeItem(key);
    } catch (_) { /* private mode */ }
  }

  function adminPage() {
    const page = (document.body && document.body.dataset.page) || "";
    return ADMIN_PAGES.includes(String(page));
  }

  function adminSession() { return read(SESSION_KEY); }

  function setAdminSession(value, username = "") {
    write(SESSION_KEY, value);
    write("oneness.adminUser", value ? username : "");
    window.dispatchEvent(new CustomEvent("ot:admin", { detail: adminSession() }));
  }

  function walletSession() { return read(WALLET_KEY); }

  function walletName() { return read(WALLET_NAME_KEY); }

  function setWalletSession(value, name = "") {
    write(WALLET_KEY, value);
    write(WALLET_NAME_KEY, value ? name : "");
    window.dispatchEvent(new CustomEvent("ot:wallet", { detail: walletSession() }));
  }

  function log(entry) {
    window.dispatchEvent(new CustomEvent("ot:api", { detail: entry }));
    const box = document.getElementById("api-log");
    if (!box) return;
    const line = document.createElement("div");
    line.className = "log-line";
    line.innerHTML =
      `<span class="st ${entry.ok ? "ok" : "bad"}">${entry.status || 0}</span>` +
      `<span class="name">${esc(entry.method)} ${esc(entry.path)}</span>` +
      `<span class="ms">${entry.ms}ms</span>`;
    box.prepend(line);
    while (box.children.length > 60) box.lastElementChild.remove();
  }

  function authHeaders(extra = {}) {
    const headers = { accept: "application/json", ...extra };
    /* The admin session is only ever created by signing in on /admin (or
       /ollama). It is attached when it exists so the operator keeps their
       rights while browsing, but no page ever asks a visitor for a token. */
    if (adminSession()) headers["x-admin-session"] = adminSession();
    if (walletSession()) headers["x-wallet-session"] = walletSession();
    return headers;
  }

  /* A hidden service is slow and a Tor circuit drops streams: a truncated
     catalog used to surface as "network error: Failed to fetch", which looks
     like a false failure because the page itself had already rendered. GETs are
     safe to replay, so they get two retries with a short backoff. */
  const RETRY_DELAYS = [400, 1200];

  async function call(path, options = {}) {
    const { method = "GET", body, headers = {}, timeout = 180000,
            retries = 2 } = options;
    const opts = { method, headers: authHeaders(headers) };
    if (body !== undefined) {
      opts.headers["content-type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    for (let attempt = 0; ; attempt += 1) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);
      const started = performance.now();
      let response, payload;
      try {
        response = await fetch(path, { ...opts, signal: controller.signal });
        const type = response.headers.get("content-type") || "";
        payload = type.includes("json")
          ? await response.json()
          : { ok: response.ok, data: await response.text(), error: null };
      } catch (problem) {
        clearTimeout(timer);
        log({ method, path, status: 0, ms: Math.round(performance.now() - started), ok: false });
        const replay = method === "GET" && problem.name !== "AbortError";
        if (replay && attempt < retries) {
          await new Promise(done => setTimeout(done, RETRY_DELAYS[attempt] ?? 1500));
          continue;
        }
        const error = new Error(problem.name === "AbortError"
          ? `timed out after ${Math.round(timeout / 1000)}s`
          : `network error: ${problem.message}`);
        error.code = "NETWORK";
        throw error;
      }
      clearTimeout(timer);
      const ms = Math.round(performance.now() - started);
      log({ method, path, status: response.status, ms, ok: !!payload.ok });
      if (!response.ok || payload.ok === false) {
        const detail = payload.error || { code: `HTTP_${response.status}`, message: "request failed" };
        const error = new Error(detail.message || "request failed");
        error.code = detail.code;
        error.status = response.status;
        error.details = detail.details;
        throw error;
      }
      return payload.data;
    }
  }

  const get = path => call(path);
  const post = (path, body) => call(path, { method: "POST", body: body || {} });
  const del = path => call(path, { method: "DELETE" });

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, ch => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }

  function toast(message, kind = "") {
    const box = document.getElementById("toasts");
    if (!box) return;
    const node = document.createElement("div");
    node.className = `toast ${kind}`.trim();
    node.innerHTML = `<b>${kind === "bad" ? "error" : kind === "ok" ? "done" : "node"}</b>${esc(message)}`;
    box.appendChild(node);
    setTimeout(() => node.remove(), kind === "bad" ? 9000 : 5200);
  }

  function fmtTor(micro) {
    const value = Number(micro || 0) / 1e8;
    if (!Number.isFinite(value)) return "0";
    return value.toFixed(8).replace(/\.?0+$/, "") || "0";
  }

  const fmtInt = value => fmt.format(Number(value || 0));

  function short(value, size = 12) {
    const text = String(value ?? "");
    return text.length > size * 2 ? `${text.slice(0, size)}…${text.slice(-6)}` : text;
  }

  function when(value) {
    if (!value) return "—";
    if (typeof value === "number") return new Date(value * 1000).toLocaleString();
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function badge(status) {
    const key = String(status || "").toLowerCase();
    const map = { paid: "paid", active: "active", deployed: "deployed", ok: "ok",
      open: "open", expired: "expired", error: "error", failed: "error" };
    return `<span class="badge ${map[key] || "info"}">${esc(status || "—")}</span>`;
  }

  /* ---------------------------------------------------------- markdown-lite */
  function inline(text) {
    return text
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  }

  function md(text) {
    const blocks = String(text || "").replace(/\r\n/g, "\n").split(/```/);
    return blocks.map((block, index) => {
      if (index % 2 === 1) {
        return `<pre class="code">${esc(block.replace(/^[a-z]*\n/i, ""))}</pre>`;
      }
      const out = [];
      let list = null;
      for (const raw of esc(block).split("\n")) {
        const line = raw.trim();
        if (!line) { if (list) { out.push(`</${list}>`); list = null; } continue; }
        const heading = line.match(/^(#{1,4})\s+(.*)$/);
        if (heading) {
          if (list) { out.push(`</${list}>`); list = null; }
          out.push(`<h3>${heading[2]}</h3>`);
          continue;
        }
        const bullet = line.match(/^[-*•]\s+(.*)$/) || line.match(/^\d+[.)]\s+(.*)$/);
        if (bullet) {
          if (list !== "ul") { if (list) out.push(`</${list}>`); out.push("<ul>"); list = "ul"; }
          out.push(`<li>${inline(bullet[1])}</li>`);
          continue;
        }
        if (list) { out.push(`</${list}>`); list = null; }
        out.push(`<p>${inline(line)}</p>`);
      }
      if (list) out.push(`</${list}>`);
      return out.join("");
    }).join("");
  }

  /* ---------------------------------------------------------------- tables */
  function table(rows, columns, empty = "nothing here yet") {
    if (!Array.isArray(rows) || rows.length === 0) {
      return `<div class="empty">${esc(empty)}</div>`;
    }
    const head = columns.map(col => `<th class="${col.cls || ""}">${esc(col.label)}</th>`).join("");
    const body = rows.map(row => "<tr>" + columns.map(col => {
      const value = col.render ? col.render(row) : esc(row[col.key] ?? "—");
      return `<td class="${col.cls || ""}">${value}</td>`;
    }).join("") + "</tr>").join("");
    return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead>` +
      `<tbody>${body}</tbody></table></div>`;
  }

  function kv(pairs) {
    return `<dl class="kv">` + pairs.filter(Boolean)
      .map(([key, value]) => `<dt>${esc(key)}</dt><dd>${value}</dd>`).join("") + `</dl>`;
  }

  const json = value => `<pre class="code">${esc(JSON.stringify(value, null, 2))}</pre>`;

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(String(text));
      toast("copied to clipboard", "ok");
    } catch (_) {
      toast("clipboard blocked by the browser", "bad");
    }
  }

  /* -------------------------------------------------------- wallet identity */
  /* Every public page reads the visitor's own wallet from the account session
     (POST /api/auth/login). No admin credential is involved anywhere. */
  async function me() {
    if (!walletSession()) return null;
    try {
      return await get("/api/auth/me");
    } catch (_) {
      return null;
    }
  }

  async function myWallet() {
    const data = await me();
    return data && data.wallet ? data.wallet : null;
  }

  /* Every wallet the account owns (max 5, generated + imported). A bare
     signed session without an account row still returns its one wallet. */
  async function myWallets() {
    if (!walletSession()) return [];
    try {
      const data = await get("/api/auth/wallets");
      return data.wallets || [];
    } catch (_) {
      const one = await myWallet();
      return one ? [one] : [];
    }
  }

  /* Fill a payment dropdown with every account wallet, keeping the primary. */
  function fillWalletSelect(select, wallets, preferred) {
    if (!select) return;
    const rows = wallets || [];
    select.disabled = rows.length === 0;
    select.innerHTML = rows.length
      ? rows.map(row => {
          const label = (row.meta && row.meta.label) ? `${row.meta.label} · ` : "";
          return `<option value="${esc(row.name)}">${esc(label)}${esc(row.name)}` +
            ` · ${fmtTor(row.balance)} TOR${row.primary ? " · primary" : ""}</option>`;
        }).join("")
      : `<option value="">sign in at /wallet</option>`;
    const wanted = preferred || walletName();
    if (wanted && [...select.options].some(option => option.value === wanted)) {
      select.value = wanted;
    }
  }

  function bindAccount() {
    const pill = document.getElementById("pill-account");
    if (!pill) return;
    const name = walletName();
    pill.textContent = name ? `wallet · ${name}` : "sign in";
    pill.title = name
      ? "your account wallet — open /wallet"
      : "create an account or sign in with username + password";
  }

  /* ---------------------------------------------------------------- models */
  async function models(select, preferred) {
    if (!select) return [];
    try {
      const data = await get("/api/ollama/models");
      const rows = data.models || [];
      select.innerHTML = rows.map(row =>
        `<option value="${esc(row.name)}">${esc(row.name)}` +
        `${row.size_gb ? ` · ${row.size_gb}GB` : ""}${row.cloud ? " · cloud" : ""}</option>`
      ).join("") || `<option value="">no models installed</option>`;
      const wanted = preferred || data.default_model;
      if (wanted && rows.some(row => row.name === wanted)) select.value = wanted;
      return rows;
    } catch (problem) {
      select.innerHTML = `<option value="">Ollama unreachable</option>`;
      return [];
    }
  }

  /* ----------------------------------------------------------- token icons */
  /* Token logos are public metadata (GET /api/tokens/images, the launcher
     window, or the native IPFS pin). When no source knows a ticker the page
     draws a deterministic identicon instead of inventing a picture. */
  function gateway(image) {
    const text = String(image || "").trim();
    if (/^ipfs:\/\//i.test(text)) {
      return "https://ipfs.io/ipfs/" + text.slice(7).replace(/^ipfs\//i, "");
    }
    return text;
  }

  function identicon(seed) {
    const text = String(seed || "TOR");
    let hash = 2166136261;
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619) >>> 0;
    }
    const hue = hash % 360;
    const cells = [];
    for (let row = 0; row < 5; row += 1) {
      for (let col = 0; col < 3; col += 1) {
        if (!((hash >>> ((row * 3 + col) % 30)) & 1)) continue;
        cells.push(`<rect x="${col}" y="${row}" width="1" height="1"/>`);
        if (4 - col !== col) cells.push(`<rect x="${4 - col}" y="${row}" width="1" height="1"/>`);
      }
    }
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 5 5" shape-rendering="crispEdges">` +
      `<rect width="5" height="5" fill="hsl(${hue} 55% 12%)"/>` +
      `<g fill="hsl(${hue} 90% 62%)">${cells.join("")}</g></svg>`;
    return `data:image/svg+xml,${encodeURIComponent(svg)}`;
  }

  function tokenIcon(row) {
    const item = row || {};
    const image = gateway(item.image);
    const source = String(item.image_source || "").trim();
    if (image && /^(https?:|data:image|\/)/i.test(image)) {
      // a logo this node mirrors (or an inline identicon) paints at once; only a
      // third-party URL is worth deferring
      return { src: image, source: source || "token metadata image",
               local: !/^https?:/i.test(image) };
    }
    return { src: identicon(item.mint_address || item.symbol),
             source: source || "generated identicon — no image stored",
             local: true };
  }

  /* ----------------------------------------------------------------- voice */
  /* Browser speech: synthesis for replies, recognition for the record button.
     Both are opt-in per surface; the switches persist in localStorage. */
  const VOICE_PREFIX = "oneness.voice.";
  const FEMALE_HINTS = ["zira", "aria", "jenny", "samantha", "hazel", "eva",
    "serena", "karen", "moira", "tessa", "fiona", "susan", "female",
    "google uk english female", "google us english"];
  const MALE_HINTS = ["david", "mark", "guy", "george", "daniel", "alex",
    "fred", "ryan", "tom", "male", "google uk english male"];

  const synth = () => window.speechSynthesis || null;
  const Recognition = () => window.SpeechRecognition || window.webkitSpeechRecognition || null;

  function supported() {
    return { speech: !!synth(), recognition: !!Recognition() };
  }

  function speakable(text) {
    return String(text || "")
      .replace(/```[\s\S]*?```/g, " code block ")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/\*\*([^*]+)\*\*/g, "$1")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, " image ")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/https?:\/\/\S+/g, " link ")
      .replace(/[#>*_|]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function voices() {
    const engine = synth();
    return engine ? (engine.getVoices() || []) : [];
  }

  function pickVoice(gender) {
    const all = voices();
    if (!all.length) return null;
    const english = all.filter(voice => /^en/i.test(String(voice.lang || "")));
    const pool = english.length ? english : all;
    const hints = gender === "male" ? MALE_HINTS : FEMALE_HINTS;
    for (const hint of hints) {
      const hit = pool.find(voice => String(voice.name).toLowerCase().includes(hint));
      if (hit) return hit;
    }
    return pool[0];
  }

  function voiceName(gender) {
    const voice = pickVoice(gender);
    return voice ? `${voice.name} · ${voice.lang}` : "the system default";
  }

  function stop() {
    const engine = synth();
    if (engine) engine.cancel();
  }

  function speak(text, options = {}) {
    const engine = synth();
    if (!engine) { toast("this browser has no speech synthesis", "bad"); return false; }
    const body = speakable(text);
    if (!body) return false;
    stop();
    const voice = pickVoice(options.gender || "female");
    const chunks = body.match(/[^.!?]{1,200}[.!?]+|[^.!?]{1,200}$/g) || [body.slice(0, 600)];
    chunks.forEach(chunk => {
      const line = chunk.trim();
      if (!line) return;
      const utterance = new SpeechSynthesisUtterance(line);
      if (voice) { utterance.voice = voice; utterance.lang = voice.lang || "en-US"; }
      utterance.rate = Number(options.rate || 1);
      utterance.pitch = Number(options.pitch
        || (options.gender === "male" ? 0.82 : 1.12));
      utterance.volume = 1;
      engine.speak(utterance);
    });
    return true;
  }

  function listen(options = {}) {
    const Ctor = Recognition();
    if (!Ctor) {
      toast("this browser cannot transcribe speech — use Chrome or Edge", "bad");
      return null;
    }
    const engine = new Ctor();
    engine.lang = options.lang || "en-US";
    engine.interimResults = true;
    engine.continuous = false;
    engine.maxAlternatives = 1;
    let final = "";
    engine.onresult = event => {
      let interim = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        if (result.isFinal) final += result[0].transcript + " ";
        else interim += result[0].transcript;
      }
      if (options.onText) options.onText(`${final}${interim}`.trim(), !!final);
    };
    engine.onerror = event => {
      const reason = event.error || "speech error";
      if (options.onError) options.onError(reason);
      else if (!["no-speech", "aborted"].includes(reason)) toast(`microphone: ${reason}`, "bad");
    };
    engine.onend = () => { if (options.onEnd) options.onEnd(final.trim()); };
    try { engine.start(); } catch (problem) { toast(problem.message, "bad"); return null; }
    return { stop: () => { try { engine.stop(); } catch (_) { /* already stopped */ } } };
  }

  function voiceSwitch(surface, fallback = true) {
    const held = read(VOICE_PREFIX + surface);
    return held === "" ? fallback : held === "on";
  }

  function setVoiceSwitch(surface, value) {
    write(VOICE_PREFIX + surface, value ? "on" : "off");
  }

  /* ---------------------------------------------------------------- stream */
  async function stream(path, body, onChunk) {
    const response = await fetch(path, {
      method: "POST",
      headers: authHeaders({ "content-type": "application/json" }),
      body: JSON.stringify(body || {}),
    });
    if (!response.ok || !response.body) {
      let message = `HTTP ${response.status}`;
      try { message = (await response.json()).error?.message || message; } catch (_) {}
      throw new Error(message);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (line.trim()) {
          try { onChunk(JSON.parse(line)); } catch (_) { onChunk({ status: line.trim() }); }
        }
      }
    }
  }

  /* ------------------------------------------------------------------- init */
  function tilt() {
    document.querySelectorAll(".stat,.feature").forEach(card => {
      card.addEventListener("pointermove", event => {
        const rect = card.getBoundingClientRect();
        const x = (event.clientX - rect.left) / rect.width - 0.5;
        const y = (event.clientY - rect.top) / rect.height - 0.5;
        card.style.transform =
          `perspective(700px) rotateY(${x * 8}deg) rotateX(${-y * 8}deg) translateY(-2px)`;
      });
      card.addEventListener("pointerleave", () => { card.style.transform = ""; });
    });
  }

  async function refreshPills() {
    const nodePill = document.getElementById("pill-node");
    const heightPill = document.getElementById("pill-height");
    const ollamaPill = document.getElementById("pill-ollama");
    try {
      const health = await get("/api/health");
      if (nodePill) {
        nodePill.className = `pill ${health.node_reachable ? "on" : "off"}`;
        nodePill.innerHTML = `node <b>${health.node_reachable ? "up" : "down"}</b>`;
      }
      if (heightPill) {
        heightPill.className = "pill";
        heightPill.innerHTML = `height <b>${health.height ?? "—"}</b>`;
      }
    } catch (_) {
      if (nodePill) { nodePill.className = "pill off"; nodePill.innerHTML = "api <b>down</b>"; }
    }
    try {
      const status = await get("/api/ollama/status");
      if (ollamaPill) {
        ollamaPill.className = `pill ${status.reachable ? "on" : "off"}`;
        ollamaPill.innerHTML =
          `ollama <b>${status.reachable ? (status.version || "up") : "down"}</b>`;
      }
    } catch (_) {
      if (ollamaPill) {
        ollamaPill.className = "pill off";
        ollamaPill.innerHTML = "ollama <b>down</b>";
      }
    }
  }

  function init() {
    bindAccount();
    tilt();
    refreshPills();
    setInterval(refreshPills, 30000);
  }

  document.addEventListener("DOMContentLoaded", init);

  return { call, get, post, del, log, me, myWallet, myWallets, fillWalletSelect,
    adminPage, esc, toast, md,
    table, kv, json, copy, fmtTor, fmtInt, short, when, badge, models, stream,
    refreshPills, tilt, adminSession, setAdminSession, walletSession,
    walletName, setWalletSession, identicon, tokenIcon,
    voice: { speak, stop, listen, supported, voiceName, pickVoice,
             enabled: voiceSwitch, remember: setVoiceSwitch } };
})();
