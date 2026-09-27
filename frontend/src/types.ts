export interface CertifiedAsset {
  kind: string;
  icon: string;
  label: string;
  url: string;
}

export interface WhoAmI {
  email: string | null;
  host: string | null;
  auth_mode: string;
  connected: boolean;
}

export interface SpaceAgentConfig {
  id: string;
  name: string;
  icon: string;
  blurb: string;
  status: "live";
  poweredBy: string;
  spaceId: string;
  hints: string[];
  example?: string | null;
}

export interface ExternalMcpAgentConfig {
  id: string;
  name: string;
  icon: string;
  blurb: string;
  status: "live";
  external: true;
  /** "custom_mcp" (Databricks App /mcp) or "mcp_service" (governed UC MCP Service). */
  kind?: string;
  poweredBy: string;
  mcp_url: string;
  tool_name: string;
  description?: string | null;
  deep_link?: string | null;
  example?: string | null;
}

export interface AppConfig {
  genie_space_agents: SpaceAgentConfig[];
  external_mcp_agents: ExternalMcpAgentConfig[];
}

export interface DonePayload {
  answer: string;
  assets: CertifiedAsset[];
  deep_link: string;
  conversation_id: string;
  via: string;
  routed_space_ids?: string[];
  agent_id?: string | null;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  assets?: CertifiedAsset[];
  deepLink?: string;
  via?: string;
  agentName?: string;
  agentIcon?: string;
  error?: boolean;
}
