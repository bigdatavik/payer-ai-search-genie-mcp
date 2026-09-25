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
}

export interface AppConfig {
  genie_space_agents: SpaceAgentConfig[];
}

export interface DonePayload {
  answer: string;
  assets: CertifiedAsset[];
  deep_link: string;
  conversation_id: string;
  via: string;
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
