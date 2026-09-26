import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { askGenie, fetchConfig, fetchWhoAmI } from "./api";
import type { CertifiedAsset, ChatMessage, WhoAmI } from "./types";

const STATUS_LABEL: Record<string, string> = {
  in_progress: "Genie One is routing across your certified agents…",
  submitted: "Sending to the agent…",
  filtering_context: "Finding the right certified agent…",
  asking_ai: "Generating SQL…",
  asking_agent: "Asking the agent over its MCP endpoint…",
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
  /** Genie Space this certified agent corresponds to — used to match which
   *  agent Genie One routed to (NOT to call it directly). */
  spaceId?: string;
  /** true = reached OUTSIDE Genie One via its own custom MCP server. */
  external?: boolean;
  /** a starter question shown on the welcome screen */
  example?: string | null;
}

/** Fallback live tile when no certified agents are configured (portable default). */
const GENIE_ONE_FALLBACK: Agent = {
  id: "genie-one",
  icon: "🧭",
  name: "Genie One",
  blurb: "All certified agents",
  status: "live",
};

/** External agents that run OUTSIDE Databricks — shown as previews until wired. */
const EXTERNAL_PREVIEWS: Agent[] = [
  { id: "claims", icon: "🧾", name: "Claims", blurb: "Status, denials & recovery", status: "preview" },
  { id: "priorauth", icon: "✅", name: "Prior Authorization", blurb: "Auth & utilization review", status: "preview" },
  { id: "network", icon: "🏥", name: "Provider Network", blurb: "Contracts & adequacy", status: "preview" },
  { id: "fwa", icon: "🛡️", name: "Fraud, Waste & Abuse", blurb: "Anomalies & SIU leads", status: "preview" },
];

/** Fallback starters when no certified agents are configured (portable default). */
const DEFAULT_EXAMPLES = [
  { icon: "📈", q: "Show the key finance KPIs for the latest closed month." },
  { icon: "👥", q: "How many members are in each risk tier?" },
  { icon: "💊", q: "What's the Rx adherence (PDC) trend for our Stars measures?" },
  { icon: "✅", q: "Which HEDIS measures have the biggest gaps in care?" },
];

export default function App() {
  const [who, setWho] = useState<WhoAmI | null>(null);
  const [agents, setAgents] = useState<Agent[]>([GENIE_ONE_FALLBACK, ...EXTERNAL_PREVIEWS]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [routedId, setRoutedId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string>("");
  const scrollRef = useRef<HTMLDivElement>(null);

  const liveAgents = agents.filter((a) => a.status === "live");
  const exampleAgents = liveAgents.filter((a) => a.example);
  const examples = exampleAgents.length
    ? exampleAgents.map((a) => ({ icon: a.icon, q: a.example as string }))
    : DEFAULT_EXAMPLES;

  useEffect(() => {
    fetchWhoAmI().then(setWho).catch(() => setWho(null));
    // Certified Databricks agents (routed by Genie One) + custom-MCP agents
    // (reached OUTSIDE Genie One) both come from config, so the app is portable.
    fetchConfig()
      .then((cfg) => {
        const certified = (cfg.genie_space_agents ?? []).map((a) => ({ ...a, status: "live" as const }));
        const external = (cfg.external_mcp_agents ?? []).map((a) => ({
          id: a.id,
          icon: a.icon,
          name: a.name,
          blurb: a.blurb,
          status: "live" as const,
          external: true,
          example: a.example,
        }));
        const base = certified.length ? certified : [GENIE_ONE_FALLBACK];
        setAgents([...base, ...external, ...EXTERNAL_PREVIEWS]);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, status]);

  async function send(question: string) {
    const q = question.trim();
    if (!q || busy) return;

    // No optimistic guess — the orchestrator LLM tells us the route authoritatively.
    setRoutedId(null);
    setInput("");
    setBusy(true);
    setStatus(STATUS_LABEL.in_progress);
    setMessages((m) => [...m, { role: "user", content: q }]);

    await askGenie(
      q,
      conversationId,
      {
        onStatus: (poll, s) => setStatus(`${STATUS_LABEL[s] ?? "Working…"} (step ${poll})`),
        // Authoritative routing: light the chosen agent's tile immediately.
        // For an external/MCP agent this is the final tile; for Genie One we
        // refine to the specific certified agent from the answer's citations.
        onRoute: (agentId) => {
          if (agentId && agents.some((a) => a.id === agentId)) setRoutedId(agentId);
        },
        onDone: (payload) => {
          setConversationId(payload.conversation_id);
          // Reconcile to the tile that actually answered: a certified Genie
          // agent (matched by routed space id) or the routed agent id itself.
          const matched =
            liveAgents.find((a) => a.spaceId && (payload.routed_space_ids ?? []).includes(a.spaceId)) ??
            agents.find((a) => a.id === payload.agent_id);
          const finalAgent = matched ?? null;
          if (finalAgent) setRoutedId(finalAgent.id);
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
              agentName: finalAgent?.name,
              agentIcon: finalAgent?.icon ?? "🧭",
            },
          ]);
        },
        onError: (message) => {
          setMessages((m) => [...m, { role: "assistant", content: `⚠️ ${message}`, error: true, agentIcon: "🧭" }]);
        },
      },
      { spaceId: null, agentName: "Genie One" } // always route via Genie One
    );

    setBusy(false);
    setStatus("");
  }

  function clearConversation() {
    setMessages([]);
    setConversationId(null);
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
                Ask anything — an <strong>orchestrator</strong> routes to Genie One or a specialized MCP agent.
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
              {!started && <Welcome examples={examples} onPick={send} disabled={busy} />}
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
                  placeholder="Ask a question — Genie One routes it to the right agent…"
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
                  AI-generated — Genie One picks the certified agent. Verify before acting.
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
  const hasCertified = agents.some((a) => a.status === "live" && a.spaceId);
  return (
    <aside className="rail">
      <div className="rail-title">{hasCertified ? "Certified Genie agents" : "Agents"}</div>
      <div className="rail-list">
        {agents.map((a) => {
          const active = a.id === routedId;
          const cls = ["agent", active ? "active" : "", active && busy ? "answering" : "", busy && !active ? "dim" : ""]
            .filter(Boolean)
            .join(" ");
          const title = a.external
            ? "Reached outside Genie One via its own custom MCP server"
            : a.status === "preview"
            ? "Runs outside Databricks — coming soon"
            : "Certified · via Genie One";
          const statusLabel = active
            ? busy
              ? "Answering…"
              : "Active"
            : a.external
            ? "MCP"
            : a.status === "live"
            ? "Live"
            : "Preview";
          const statusClass = active ? "active" : a.external ? "external" : a.status;
          return (
            <div key={a.id} className={cls} title={title}>
              <span className="agent-icon" aria-hidden>
                {a.icon}
              </span>
              <span className="agent-text">
                <span className="agent-name">{a.name}</span>
                <span className="agent-blurb">{a.blurb}</span>
              </span>
              <span className={`agent-status ${statusClass}`}>{statusLabel}</span>
            </div>
          );
        })}
      </div>
      <div className="rail-foot">
        <span className="rail-foot-title">🧭 LLM orchestrator</span>
        Ask anything — an LLM picks the right agent: <strong>Genie One</strong> for certified
        workspace data, or a <strong>◆ custom MCP</strong> agent reached outside Genie One. No
        picking, no keywords. Preview tiles are external agents not yet wired.
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

function Welcome({
  examples,
  onPick,
  disabled,
}: {
  examples: { icon: string; q: string }[];
  onPick: (q: string) => void;
  disabled: boolean;
}) {
  return (
    <div className="welcome">
      <span className="welcome-badge">🧭 One box · every certified agent</span>
      <h2>Ask about your payer data</h2>
      <p className="welcome-sub">
        Just ask. Genie One reads your question, activates the right certified Genie agent, and
        answers — you don't choose an agent or hunt for the data.
      </p>
      <div className="examples">
        {examples.map((ex) => (
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
      <span className="avatar">{msg.agentIcon ?? "🧭"}</span>
      <div className={`bubble assistant-bubble ${msg.error ? "error" : ""}`}>
        {msg.agentName && (
          <div className="answered-by">
            <span className="answered-dot" />
            {msg.agentName}
            {msg.via ? ` · via ${msg.via}` : ""}
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
