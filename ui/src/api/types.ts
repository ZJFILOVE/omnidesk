import type { ContentDomain, PluginConfigField } from '../types';

export interface TauriCoreApi {
  invoke<T = unknown>(
    cmd: string,
    args?: Record<string, unknown>,
    options?: unknown,
  ): Promise<T>;
}

export interface TauriEvent<T = unknown> {
  event: string;
  id: number;
  payload: T;
}

export type TauriUnlistenFn = () => void | Promise<void>;

export interface TauriEventApi {
  listen<T = unknown>(
    event: string,
    handler: (event: TauriEvent<T>) => void,
    options?: unknown,
  ): Promise<TauriUnlistenFn>;
}

export interface TauriGlobal {
  core?: TauriCoreApi;
  event?: TauriEventApi;
}

declare global {
  interface Window {
    __TAURI__?: TauriGlobal;
  }
}

export type ControlOp =
  | 'ping'
  | 'health'
  | 'core.ping'
  | 'capability.list'
  | 'capability.call'
  | 'domain.snapshot'
  | 'domain.invoke'
  | 'ui.action'
  | 'bus.publish'
  | 'bus.emit'
  | 'job.submit'
  | 'job.list'
  | 'job.get'
  | 'job.cancel'
  | 'search.query'
  | 'plugin.list'
  | 'plugin.load'
  | 'plugin.unload'
  | 'plugin.reload'
  | 'plugin.verify'
  | 'plugin.health';

export type SourceDomain = 'media' | 'text' | 'book' | 'recommend' | 'core';

export interface ListPluginsArgs {
  liveOnly?: boolean;
  live_only?: boolean;
}

export interface CallCapabilityArgs {
  capability: string;
  payload?: Record<string, unknown>;
  providerPluginId?: string;
  provider_plugin_id?: string;
  timeoutMs?: number;
  timeout_ms?: number;
}

export interface EmitEventArgs {
  topic: string;
  payload?: Record<string, unknown>;
  timeoutMs?: number;
  timeout_ms?: number;
}

export interface ReadSourceArgs {
  domain: SourceDomain;
  sourceId?: string;
  source_id?: string;
  payload?: Record<string, unknown>;
  timeoutMs?: number;
  timeout_ms?: number;
}

export interface WorkspaceTabSnapshot {
  tab_id: string;
  plugin_id: string;
  title: string;
  domain: ContentDomain;
  badge?: string;
  ui_schema?: Record<string, unknown> | Array<Record<string, unknown>>;
}

export interface DockWidgetSnapshot {
  widget_id: string;
  plugin_id: string;
  title: string;
  supported_domains?: ContentDomain[];
  ui_schema?: Record<string, unknown> | Array<Record<string, unknown>>;
}

export interface WorkspaceSnapshotData {
  workspace_tabs: WorkspaceTabSnapshot[];
  dock_widgets: DockWidgetSnapshot[];
  media_sources?: Array<Record<string, unknown>>;
  text_sources?: Array<Record<string, unknown>>;
  book_sources?: Array<Record<string, unknown>>;
}

export interface WorkspaceUiActionArgs {
  kind: 'workspace_tab' | 'dock_widget';
  specId: string;
  actionId: string;
  pluginId?: string;
  state?: Record<string, unknown>;
  timeoutMs?: number;
}

export interface CallRawArgs {
  op: ControlOp;
  params?: Record<string, unknown>;
  timeoutMs?: number;
  timeout_ms?: number;
}

export interface MpvProbeStatus {
  available: boolean;
  initialized: boolean;
  backend: string;
  hwdec: string;
  hwdecActive?: boolean;
  hwdecCurrent?: string;
  status: string;
  detail: string;
  executablePath?: string | null;
  executableVersion?: string | null;
  ipcSupported?: boolean;
  libmpvAvailable?: boolean;
  libmpvVersion?: string | null;
  libmpvLinked?: boolean;
  unavailableReason?: string | null;
}

export interface MpvPlaybackState {
  available: boolean;
  initialized: boolean;
  playing: boolean;
  paused: boolean;
  stopped: boolean;
  buffering: boolean;
  positionSec: number;
  durationSec: number;
  speed: number;
  volume: number;
  videoWidth: number;
  videoHeight: number;
  fps: number;
  videoBitrate: number;
  decoderDropFrames: number;
  outputDropFrames: number;
  videoCodec: string;
  audioCodec: string;
  audioSamplerate: number;
  audioChannels: number;
  cacheDurationSec: number;
  hwdecCurrent: string;
  hwdecActive: boolean;
  lastError: string;
  currentUrl: string;
  itemId: string;
  title: string;
  artist: string;
  album: string;
  coverUrl: string;
  ipcSocketPath?: string | null;
}

export interface MpvOpenRequest {
  url: string;
  audioUrl?: string;
  subtitleUrls?: string[];
  httpHeaders?: Record<string, string>;
  httpProxy?: string;
  referrer?: string;
  userAgent?: string;
  startSeconds?: number;
  endSeconds?: number;
  hwdec?: boolean;
  paused?: boolean;
  title?: string;
  artist?: string;
  album?: string;
  coverUrl?: string;
  itemId?: string;
}

export interface MpvEmbedProbe {
  available: boolean;
  backend: string;
  apiVersion?: string | null;
  detail: string;
  unavailableReason?: string | null;
}

export interface MpvFrameEnvelope {
  seq: number;
  width: number;
  height: number;
  mime: string;
  data: string;
}

export interface MprisStatus {
  enabled: boolean;
  busName: string;
  status: string;
}

export interface RuntimeStatus {
  shell: string;
  appId: string;
  version: string;
  platform: string;
  status: string;
  mpv: MpvProbeStatus;
  mpris: MprisStatus;
}

export interface EventBridgeSnapshot {
  status: 'offline' | 'degraded' | 'connected' | string;
  supported: boolean;
  reason: string;
  socket_path: string | null;
  forwarded_count: number;
  last_event_topic: string | null;
  last_checked_epoch_ms: number;
  bus_event_channel: string;
  status_event_channel: string;
}

export interface CoreStatusData {
  ok?: boolean;
  online: boolean;
  degraded: boolean;
  socket_path?: string;
  status?: string;
  capabilities?: number;
  plugins?: number;
  event_bridge?: EventBridgeSnapshot;
}

export interface BackendPluginRecord {
  plugin_id: string;
  plugin_dir?: string;
  state: 'loaded' | 'discovered' | 'unloaded' | 'faulted' | 'invalid' | string;
  enabled?: boolean;
  error?: string;
  loaded_at?: number | null;
  reload_count?: number;
  name?: string;
  version?: string;
  kind?: 'provider' | 'workspace' | 'hybrid' | 'extension' | string;
  domain?: ContentDomain | string;
  description?: string;
  author?: string;
  provides?: string[];
  consumes?: string[];
  permissions?: string[];
  config_schema?: PluginConfigField[];
}

export interface ListPluginsData {
  plugins: BackendPluginRecord[];
  source: 'control_socket' | 'manifest_scan' | string;
  core_online: boolean;
  degraded: boolean;
  notice?: string;
}

export interface EmitEventData {
  event_id: string;
  topic: string;
  delivered: number;
  failed: number;
}

export interface BusEventEnvelope {
  topic: string;
  payload: Record<string, unknown>;
  event_id: string;
  source_plugin_id: string;
  timestamp: number | null;
}

export interface AiAgentRequestPayload {
  agent_id?: string;
  action?:
    | 'chat'
    | 'summarize'
    | 'translate'
    | 'etymology'
    | 'parse_sentence'
    | 'clause_parse'
    | 'qa'
    | 'custom';
  session_action?: 'list' | 'get' | 'delete' | 'rename' | 'export';
  session_id?: string;
  session_title?: string;
  session_format?: 'markdown' | 'json';
  domain?: ContentDomain;
  context?: string;
  context_title?: string;
  context_refs?: Array<{
    capability: string;
    provider_plugin_id?: string;
    label?: string;
    payload?: Record<string, unknown>;
  }>;
  system_prompt?: string;
  selection?: string;
  prompt?: string;
  messages?: Array<{ role: 'system' | 'user' | 'assistant'; content: string }>;
  route_override?: string;
  temperature?: number;
  timeout_ms?: number;
  limit?: number;
}

export interface AgentSessionMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  created_at: string;
  action?: string;
  domain?: string;
  served_model?: string;
  route_group?: string;
  is_error?: boolean;
  error_code?: string;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
  };
}

export interface AgentSessionSummary {
  id: string;
  session_id: string;
  title: string;
  domain: string;
  context_title: string;
  message_count: number;
  created_at: string;
  updated_at: string;
  model: string;
  served_model: string;
  route_group: string;
  last_message: string;
  last_role: string;
}

export interface AgentSessionRecord extends AgentSessionSummary {
  schema_version: number;
  messages: AgentSessionMessage[];
  context_refs?: Array<{ capability: string; label: string }>;
}

export interface AgentSessionExport {
  session_id: string;
  format: 'markdown' | 'json';
  mime_type: string;
  filename: string;
  content: string;
}

export interface AiAgentResponseData {
  reply?: string;
  structured?: unknown;
  served_model?: string;
  model?: string;
  route_group?: string;
  action?: string;
  domain?: string;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
  };
  session_id?: string;
  session?: AgentSessionSummary & { messages?: AgentSessionMessage[] };
  sessions?: AgentSessionSummary[];
  persisted?: boolean;
  user_message_id?: string;
  assistant_message_id?: string;
  count?: number;
  deleted?: boolean;
  format?: 'markdown' | 'json';
  mime_type?: string;
  filename?: string;
  content?: string;
}

export interface LlmChatRequestPayload {
  system_prompt?: string;
  prompt?: string;
  messages?: Array<{ role: 'system' | 'user' | 'assistant'; content: string }>;
  route_override?: string;
  model?: string;
  reasoning_effort?: string;
  stream?: boolean;
  temperature?: number;
  timeout_ms?: number;
}

export interface LlmChatResponseData {
  reply: string;
  model?: string;
  served_model?: string;
  route_group?: string;
  route_used?: string;
  finish_reason?: string;
  streaming?: boolean;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
  };
}

export interface RpcErrorInfo {
  code: string;
  message: string;
  providerPluginId?: string;
  data?: Record<string, unknown>;
}

export type RpcResult<T = Record<string, unknown>> =
  | {
      available: false;
      ok: false;
      error: RpcErrorInfo;
      providerPluginId?: string;
      data?: undefined;
    }
  | {
      available: true;
      ok: true;
      data: T;
      providerPluginId: string;
      error: null;
    }
  | {
      available: true;
      ok: false;
      error: RpcErrorInfo;
      providerPluginId: string;
      data?: Record<string, unknown>;
    };
