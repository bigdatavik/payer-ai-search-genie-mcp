import type { AppConfig, DonePayload, WhoAmI } from "./types";

export async function fetchWhoAmI(): Promise<WhoAmI> {
  const res = await fetch("api/whoami");
  if (!res.ok) throw new Error(`whoami failed: ${res.status}`);
  return res.json();
}

export async function fetchConfig(): Promise<AppConfig> {
  const res = await fetch("api/config");
  if (!res.ok) throw new Error(`config failed: ${res.status}`);
  return res.json();
}

export interface AskHandlers {
  onStatus?: (poll: number, status: string) => void;
  /** Authoritative routing signal from the orchestrator (which tool/agent it chose). */
  onRoute?: (agentId: string | null, agentName: string, via: string) => void;
  onDone: (payload: DonePayload) => void;
  onError: (message: string) => void;
}

/**
 * POST a question to /api/ask and consume the Server-Sent Events stream.
 *
 * We use fetch + a stream reader (not EventSource) because EventSource only
 * supports GET — we need to POST the question and conversation_id in the body.
 */
export interface AskOptions {
  spaceId?: string | null;
  agentName?: string;
}

export async function askGenie(
  question: string,
  conversationId: string | null,
  handlers: AskHandlers,
  opts: AskOptions = {}
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        conversation_id: conversationId,
        space_id: opts.spaceId ?? null,
        agent_name: opts.agentName ?? null,
      }),
    });
  } catch (e) {
    handlers.onError(`Network error contacting the app: ${String(e)}`);
    return;
  }

  if (!res.ok || !res.body) {
    handlers.onError(`Request failed: ${res.status} ${res.statusText}`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // Parse the SSE frames as they arrive: frames are separated by a blank line,
  // each has "event: <name>" and "data: <json>" lines.
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      dispatchFrame(frame, handlers);
    }
  }
}

function dispatchFrame(frame: string, handlers: AskHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (dataLines.length === 0) return;

  let data: any;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return;
  }

  if (event === "status") handlers.onStatus?.(data.poll, data.status);
  else if (event === "route") handlers.onRoute?.(data.agent_id ?? null, data.agent_name, data.via);
  else if (event === "done") handlers.onDone(data as DonePayload);
  else if (event === "error") handlers.onError(data.message ?? "Unknown error");
}
