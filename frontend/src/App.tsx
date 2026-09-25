import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { askGenie, fetchConfig, fetchWhoAmI } from "./api";
import type { CertifiedAsset, ChatMessage, WhoAmI } from "./types";

const STATUS_LABEL: Record<string, string> = {
  in_progress: "Discovering the certified asset…",
  submitted: "Sending to the agent…",
  filtering_context: "Finding the right tables…",
  asking_ai: "Generating SQL…",
  pending_warehouse: "Waiting for the warehouse…",
  fetching_metadata: "Gathering context…",
  executing_query: "Running the query…",
  completed: "Done",
};

interface Agent {
  id: string;
  icon: string;
  name: string;
  blurb: string;
  status: "live" | "preview";
  poweredBy?: string;
  spaceId?: string;
  /** lowercase keywords that route a question to this agent */
  hints?: string[];
}

/**
 * Built-in roster. The Finance agent is always live via **Genie One** — the
 * managed MCP server that discovers across *every* certified Genie agent /
 * Genie space in the workspace, so it works in ANY workspace with zero config
 * and no asset IDs. The remaining tiles are illustrative previews (they stand
 * in for agents hosted outside Databricks).
 *
 * Extra live agents backed by a specific Genie Space are loaded at runtime from
 * /api/config (driven by the GENIE_SPACE_AGENTS env var) — nothing
 * workspace-specific is hardcoded here.
 */
const BASE_AGENTS: Agent[] = [
  {
    id: "finance",
    icon: "📊",
    name: "Finance",
    blurb: "KPIs, variance & PMPM",
    status: "live",
    poweredBy: "Genie One",
    hints: [
      "pmpm", "cost", "variance", "forecast", "kpi", "revenue", "margin",
      "financial", "finance", "budget", "spend", "mlr", "premium", "profit", "ebitda", "trend",
    ],
  },
  { id: "claims", icon: "🧾", name: "Claims", blurb: "Status, denials & recovery", status: "preview" },
  { id: "priorauth", icon: "✅", name: "Prior Authorization", blurb: "Auth & utilization review", status: "preview" },
  { id: "pharmacy", icon: "💊", name: "Pharmacy", blurb: "Drug spend & adherence", status: "preview" },
  { id: "network", icon: "🏥", name: "Provider Network", blurb: "Contracts & adequacy", status: "preview" },
  { id: "fwa", icon: "🛡️", name: "Fraud, Waste & Abuse", blurb: "Anomalies & SIU leads", status: "preview" },
];

/** Auto-route a question to a live agent. Sticky to the last agent for
 *  ambiguous follow-ups so a conversation stays with one agent. */
function routeAgent(question: string, lastId: string | null, liveAgents: Agent[]): string {
  const q = question.toLowerCase();
  for (const a of liveAgents) {
    if (a.hints?.some((h) => q.includes(h))) return a.id;
  }
  return lastId ?? liveAgents[0]?.id ?? "finance";
}

const WELCOME_EXAMPLES = [
  { icon: "📈", q: "Show the key finance KPIs for the latest closed month." },
  { icon: "👥", q: "How many members are in each risk tier?" },
  { icon: "🔍", q: "Why is Medical Cost PMPM above forecast, and what are the top drivers?" },
  { icon: "⚠️", q: "Which members have the most open care gaps?" },
];

export default function App() {
  const [who, setWho] = useState<WhoAmI | null>(null);
  const [agents, setAgents] = useState<Agent[]>(BASE_AGENTS);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [convByAgent, setConvByAgent] = useState<Record<string, string | null>>({});
  const [routedId, setRoutedId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string>("");
  const scrollRef = useRef<HTMLDivElement>(null);

  const liveAgents = agents.filter((a) => a.status === "live");

  useEffect(() => {
    fetchWhoAmI().then(setWho).catch(() => setWho(null));
    // Merge any configured Genie Space agents in right after Finance.
    fetchConfig()
      .then((cfg) => {
        const extra = cfg.genie_space_agents ?? [];
        if (extra.length) {
          setAgents((prev) => {
            const previews = prev.filter((a) => a.status !== "live");
            const live = prev.filter((a) => a.status === "live");
            return [...live, ...extra, ...previews];
          });
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, status]);

  async function send(question: string) {
    const q = question.trim();
    if (!q || busy) return;

    const targetId = routeAgent(q, routedId, liveAgents);
    const target = agents.find((a) => a.id === targetId) ?? liveAgents[0];
    setRoutedId(targetId);
    setInput("");
    setBusy(true);
    setStatus(`Supervisor routing to ${target.name}…`);
    setMessages((m) => [...m, { role: "user", content: q }]);

    await askGenie(
      q,
      convByAgent[targetId] ?? null,
      {
        onStatus: (poll, s) => {
          setStatus(`${STATUS_LABEL[s] ?? "Working…"} (step ${poll})`);
        },
        onDone: (payload) => {
          setConvByAgent((c) => ({ ...c, [targetId]: payload.conversation_id }));
          const content = payload.deep_link
            ? `${payload.answer}\n\n[Explore in Databricks](${payload.deep_link})`
            : payload.answer;
          setMessages((m) => [
            ...m,
            {
              role: "assistant",
              content,
              assets: payload.assets,
              deepLink: payload.deep_link,
              via: payload.via,
              agentName: target.name,
              agentIcon: target.icon,
            },
          ]);
        },
        onError: (message) => {
          setMessages((m) => [
            ...m,
            { role: "assistant", content: `⚠️ ${message}`, error: true, agentName: target.name, agentIcon: target.icon },
          ]);
        },
      },
      { spaceId: target.spaceId ?? null, agentName: target.name }
    );

    setBusy(false);
    setStatus("");
  }

  function clearConversation() {
    setMessages([]);
    setConvByAgent({});
    setRoutedId(null);
    setStatus("");
  }

  const started = messages.length > 0;
  const routedAgent = agents.find((a) => a.id === routedId) ?? null;

  return (
    <div className="app">
      <header className="header">
        <div className="header-inner">
          <div className="brand">
            <span className="logo" aria-hidden>
              📊
            </span>
            <div className="brand-text">
              <h1>Payer AI Search</h1>
              <p className="tagline">
                Ask anything — the <strong>Supervisor</strong> routes you to the right certified
                agent.
              </p>
            </div>
          </div>
          <ConnectionBadge who={who} />
        </div>
      </header>

      <div className="body">
        <AgentRail agents={agents} routedId={routedId} busy={busy} />

        <div className="main">
          <main className="chat" ref={scrollRef}>
            <div className="chat-inner">
              {!started && <Welcome onPick={send} disabled={busy} />}
              {messages.map((msg, i) => (
                <MessageBubble key={i} msg={msg} />
              ))}
              {busy && <Thinking status={status} agent={routedAgent} />}
            </div>
          </main>

          <footer className="composer">
            <div className="composer-inner">
              <div className="composer-box">
                <textarea
                  value={input}
                  placeholder="Ask a question — we'll route it to the right agent…"
                  rows={1}
                  disabled={busy}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      send(input);
                    }
                  }}
                />
                <button
                  className="send"
                  onClick={() => send(input)}
                  disabled={busy || !input.trim()}
                  aria-label="Ask"
                >
                  {busy ? <span className="spinner" /> : <SendIcon />}
                </button>
              </div>
              <div className="composer-meta">
                <span className="disclaimer">
                  AI-generated — the Supervisor picks the agent. Verify before acting.
                </span>
                {started && (
                  <button className="link-btn" onClick={clearConversation} disabled={busy}>
                    Clear conversation
                  </button>
                )}
              </div>
            </div>
          </footer>
        </div>
      </div>
    </div>
  );
}

function AgentRail({
  agents,
  routedId,
  busy,
}: {
  agents: Agent[];
  routedId: string | null;
  busy: boolean;
}) {
  return (
    <aside className="rail">
      <div className="rail-title">Agents</div>
      <div className="rail-list">
        {agents.map((a) => {
          const active = a.id === routedId;
          const cls = ["agent", active ? "active" : "", active && busy ? "answering" : "", busy && !active ? "dim" : ""]
            .filter(Boolean)
            .join(" ");
          return (
            <div key={a.id} className={cls} title={a.status === "preview" ? "Coming soon" : a.poweredBy}>
              <span className="agent-icon" aria-hidden>
                {a.icon}
              </span>
              <span className="agent-text">
                <span className="agent-name">{a.name}</span>
                <span className="agent-blurb">{a.blurb}</span>
              </span>
              <span className={`agent-status ${active ? "active" : a.status}`}>
                {active ? (busy ? "Answering…" : "Active") : a.status === "live" ? "Live" : "Preview"}
              </span>
            </div>
          );
        })}
      </div>
      <div className="rail-foot">
        <span className="rail-foot-title">🧭 Supervisor Agent</span>
        Reads each question and activates the right certified agent — you never have to pick. Two
        are live today; more are on the way.
      </div>
    </aside>
  );
}

function ConnectionBadge({ who }: { who: WhoAmI | null }) {
  if (!who) return null;
  const obo = who.auth_mode === "on-behalf-of-user";
  const host = who.host?.replace(/^https?:\/\//, "") ?? "—";
  return (
    <div className="conn">
      <span className={`dot ${who.connected ? "ok" : "bad"}`} />
      <div className="conn-text">
        <span className="conn-line">{who.email ?? "Not signed in"}</span>
        <span
          className="conn-sub"
          title={obo ? "Runs with your Unity Catalog permissions" : "Runs as the app service principal"}
        >
          {obo ? "on-behalf-of-you" : "service principal"} · {host}
        </span>
      </div>
    </div>
  );
}

function Welcome({ onPick, disabled }: { onPick: (q: string) => void; disabled: boolean }) {
  return (
    <div className="welcome">
      <span className="welcome-badge">🧭 One box · many certified agents</span>
      <h2>Ask about your payer data</h2>
      <p className="welcome-sub">
        Just ask. The Supervisor reads your question, activates the right certified agent, and
        answers — you don't choose an agent or hunt for the data.
      </p>
      <div className="examples">
        {WELCOME_EXAMPLES.map((ex) => (
          <button key={ex.q} className="example" onClick={() => onPick(ex.q)} disabled={disabled}>
            <span className="example-icon" aria-hidden>
              {ex.icon}
            </span>
            {ex.q}
          </button>
        ))}
      </div>
    </div>
  );
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  if (msg.role === "user") {
    return (
      <div className="row user">
        <div className="bubble user-bubble">{msg.content}</div>
      </div>
    );
  }
  return (
    <div className="row assistant">
      <span className="avatar">{msg.agentIcon ?? "🧞"}</span>
      <div className={`bubble assistant-bubble ${msg.error ? "error" : ""}`}>
        {msg.agentName && (
          <div className="answered-by">
            <span className="answered-dot" />
            {msg.agentName} agent{msg.via ? ` · ${msg.via}` : ""}
          </div>
        )}
        <div className="markdown">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
            }}
          >
            {msg.content}
          </ReactMarkdown>
        </div>
        {msg.assets && msg.assets.length > 0 && (
          <CertifiedAssets assets={msg.assets} via={msg.via ?? "Genie One"} />
        )}
      </div>
    </div>
  );
}

function CertifiedAssets({ assets, via }: { assets: CertifiedAsset[]; via: string }) {
  const agent = assets.find((a) => a.kind === "Genie agent");
  const showArrow = agent && agent.label !== via;
  return (
    <div className="assets">
      <div className="assets-header">
        ✅ Answered via <strong>{via}</strong>
        {showArrow && (
          <>
            {" → "}
            <strong>{agent!.label}</strong>
          </>
        )}
      </div>
      <div className="asset-chips">
        {assets.map((a, i) => (
          <a key={i} className="chip" href={a.url} target="_blank" rel="noreferrer">
            <span className="chip-icon">{a.icon}</span>
            <span className="chip-kind">{a.kind}</span>
            <span className="chip-label">{a.label}</span>
          </a>
        ))}
      </div>
    </div>
  );
}

function SendIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M12 19V5M12 5l-6 6M12 5l6 6"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Thinking({ status, agent }: { status: string; agent: Agent | null }) {
  return (
    <div className="row assistant">
      <span className="avatar">{agent?.icon ?? "🧭"}</span>
      <div className="bubble assistant-bubble thinking">
        <div className="dots">
          <span />
          <span />
          <span />
        </div>
        <span className="thinking-text">{status || "Working…"}</span>
      </div>
    </div>
  );
}
