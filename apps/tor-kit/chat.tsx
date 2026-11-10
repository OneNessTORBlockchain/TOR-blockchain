/** AI-NFT ChatBox — the node's ChatBox, answered by OpenRouter free models. */
import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Model } from "./api";
import { useToast } from "./ui";

type Message = { role: "user" | "assistant"; content: string };

type Props = {
  title?: string;
  hint?: string;
  nft?: Record<string, unknown> | null;
  system?: string;
  compact?: boolean;
};

export function ChatBox({ title = "AI-NFT ChatBox", hint, nft = null, system, compact }: Props) {
  const [models, setModels] = useState<Model[]>([]);
  const [model, setModel] = useState("");
  const [source, setSource] = useState("");
  const [prompt, setPrompt] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const thread = useRef<HTMLDivElement | null>(null);
  const toast = useToast();

  useEffect(() => {
    api
      .get<{ models: Model[]; default: string; source: string }>("/api/ai/models")
      .then((data) => {
        setModels(data.models || []);
        setModel(data.default || data.models?.[0]?.id || "");
        setSource(data.source || "");
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    thread.current?.scrollTo({ top: thread.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  async function send(event: React.FormEvent) {
    event.preventDefault();
    const text = prompt.trim();
    if (!text || busy) return;
    const history: Message[] = [...messages, { role: "user", content: text }];
    setMessages(history);
    setPrompt("");
    setBusy(true);
    try {
      const answer = await api.post<{ answer: string; model: string }>(
        "/api/ai/chat",
        { messages: history, model, nft, system },
        120_000,
      );
      setMessages([...history, { role: "assistant", content: answer.answer || "(empty answer)" }]);
    } catch (problem) {
      const text2 = problem instanceof Error ? problem.message : "the model did not answer";
      setMessages([...history, { role: "assistant", content: `⚠ ${text2}` }]);
      toast(text2, true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel stack">
      <div className="spread">
        <div>
          <h2 style={{ margin: 0 }}>{title}</h2>
          <p className="muted tiny" style={{ margin: "4px 0 0" }}>
            {hint || "Free OpenRouter models, proxied by this node — the key never reaches your browser."}
          </p>
        </div>
        <span className="pill">{source === "openrouter" ? `${models.length} free models` : source || "gateway"}</span>
      </div>

      <div className="field">
        <label>Model</label>
        <select value={model} onChange={(event) => setModel(event.target.value)}>
          {models.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name} · {item.context_length ? `${Math.round(item.context_length / 1000)}k ctx` : "ctx n/a"}
            </option>
          ))}
        </select>
      </div>

      <div className="chat">
        <div className="thread" ref={thread} style={compact ? { maxHeight: 220 } : undefined}>
          {messages.length === 0 ? (
            <div className="msg assistant">
              Ask about the chain, a token, a pool or this NFT. Every number comes from the node's own
              market engine, never from the model's memory.
            </div>
          ) : null}
          {messages.map((message, index) => (
            <div key={index} className={`msg ${message.role}`}>
              {message.content}
            </div>
          ))}
          {busy ? <div className="msg assistant">thinking…</div> : null}
        </div>
        <form className="composer" onSubmit={send}>
          <input value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Ask the AI-NFT…" />
          <button className="primary" disabled={busy || !prompt.trim()}>
            Send
          </button>
        </form>
      </div>
    </div>
  );
}
