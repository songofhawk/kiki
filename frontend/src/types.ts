export type KikiStatus = 'idle' | 'thinking' | 'running' | 'waiting_input';

export type TopicStatus = 'active' | 'archived' | 'deleted';

export type MessageRole = 'user' | 'assistant' | 'system';

export type RunStatus =
  | 'queued'
  | 'running'
  | 'waiting_input'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface HealthResponse {
  status: string;
  model?: string;
  database_path?: string;
  api_key_configured?: boolean;
  workspace_root?: string;
}

export interface WorkspaceMount {
  id: string;
  scope: 'global' | 'topic';
  topic_id?: string | null;
  path: string;
  label?: string | null;
  is_primary: boolean;
  exists: boolean;
  created_at: string;
}

export interface GlobalWorkspace {
  workspace_root: string;
  rules_dir: string;
  shared_dir: string;
  topics_dir: string;
  global_mounts: WorkspaceMount[];
}

export interface TopicWorkspace {
  workspace_root: string;
  rules_dir: string;
  shared_dir: string;
  topic_id: string;
  topic_dir: string;
  topic_files_dir: string;
  topic_artifacts_dir: string;
  primary_dir: string;
  extra_dirs: string[];
  global_mounts: WorkspaceMount[];
  topic_mounts: WorkspaceMount[];
}

export interface Topic {
  id: string;
  title: string;
  summary?: string | null;
  status: TopicStatus;
  workspace_dir?: string | null;
  created_at: string;
  updated_at: string;
  message_count?: number;
  last_message_at?: string | null;
  unread?: boolean;
  active_run_count?: number;
}

export interface RouteDecision {
  path: 'fast' | 'heavy';
  topic_action: 'reuse' | 'create' | 'none';
  topic_id?: string | null;
  new_topic_title?: string | null;
  agent_type?: string | null;
  confidence: number;
  reasoning?: string | null;
}

export interface HitlOption {
  label: string;
  value: string;
  approved: boolean;
  style?: 'primary' | 'danger' | 'secondary' | string;
}

export interface HitlRequest {
  run_id?: string;
  topic_id?: string;
  prompt: string;
  kind?: 'approval' | 'desktop_takeover';
  instructions?: string | null;
  url?: string | null;
  options: HitlOption[];
}

export interface StepLog {
  id: number | string;
  run_id: string;
  step_number: number;
  action_type: string;
  detail: string;
  status: 'success' | 'failed' | 'in_progress';
  created_at: string;
}

export interface Artifact {
  id: string;
  run_id: string;
  file_path: string;
  file_type: string;
  description?: string | null;
  created_at: string;
}

export interface RawLogLine {
  run_id?: string;
  line: string;
  timestamp: string;
  stream: 'stdout' | 'stderr' | 'ndjson' | string;
}

export interface SessionRun {
  id: string;
  topic_id: string;
  trigger_message_id: string;
  agent_type: string;
  status: RunStatus;
  current_step?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
  step_logs?: StepLog[];
  artifacts?: Artifact[];
  raw_logs?: RawLogLine[];
  hitl_request?: HitlRequest | null;
}

export interface QuotedMessageSnapshot {
  id: string;
  role: MessageRole;
  content: string;
  topic_id: string;
  topic_title?: string | null;
  created_at?: string | null;
}

export interface MessageMetadata {
  confidence?: number;
  reasoning?: string;
  path?: 'fast' | 'heavy';
  session_run_id?: string;
  streaming?: boolean;
  quoted_message_id?: string;
  quoted_message?: QuotedMessageSnapshot;
  [key: string]: unknown;
}

export interface Message {
  id: string;
  topic_id: string;
  role: MessageRole;
  content: string;
  metadata?: MessageMetadata | null;
  created_at: string;
  topic?: Topic | null;
  session_run?: SessionRun | null;
  streaming?: boolean;
}

export interface MessagePage {
  messages: Message[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface AdapterInfo {
  id: string;
  name: string;
  vendor: string;
  description: string;
  category: 'builtin' | 'cli' | 'hybrid';
  available: boolean;
  binary_path?: string | null;
  install_hint: string;
  default_model: string;
  models: Record<string, string>;
  supports_resume: boolean;
  supports_streaming: boolean;
  supports_hitl: boolean;
  aliases: string[];
}

export interface ChatRequest {
  content: string;
  focus_topic_id?: string | null;
  quoted_message_id?: string | null;
  force_path?: 'fast' | 'heavy' | null;
  agent_type?: string | null;
  model?: string | null;
  thinking_level?: 'low' | 'medium' | 'high' | null;
}

export interface ChatResponse {
  user_message: Message;
  route_decision?: RouteDecision | null;
  topic: Topic;
  assistant_message?: Message | null;
  session_run?: SessionRun | null;
  reassigned_messages?: Message[];
}

export interface ReassignTopicPayload {
  action: 'move' | 'split';
  target_topic_id?: string;
  new_topic_title?: string;
}

export interface ReassignTopicResponse {
  message: Message;
  topic: Topic;
}

export interface BatchReassignTopicPayload {
  message_ids: string[];
  action: 'move' | 'split';
  target_topic_id?: string;
  new_topic_title?: string;
}

export interface BatchReassignTopicResponse {
  messages: Message[];
  topic: Topic;
}

export interface ResumeRunPayload {
  decision: string;
  approved: boolean;
  comment?: string;
}

export interface DirectoryEntry {
  name: string;
  path: string;
}

export interface DirectoryBrowseResponse {
  current_path: string;
  parent_path?: string | null;
  entries: DirectoryEntry[];
  shortcuts: DirectoryEntry[];
}

export interface ToastNotification {
  id: string;
  topic_id: string;
  topic_title: string;
  run_id: string;
  summary: string;
  created_at: number;
}

export type DesktopControlMode = 'view' | 'control';

export interface DesktopDisplayInfo {
  index: number;
  display_id: number;
  name: string;
  width: number;
  height: number;
  origin_x: number;
  origin_y: number;
  is_main: boolean;
}

export interface DesktopSpaceInfo {
  index: number;
  space_id: string;
  display_index: number;
  name: string;
  is_active: boolean;
  is_fullscreen: boolean;
}

export interface DesktopPermissions {
  screen_recording: boolean;
  accessibility: boolean;
  warnings: string[];
}

export interface DesktopSession {
  id: string;
  control_mode: DesktopControlMode;
  display_index: number;
  fps: number;
  quality: number;
  max_width: number;
  idle_timeout_seconds: number;
  created_at: string;
  last_activity_at: string;
  expires_at: string;
  run_id?: string | null;
  topic_id?: string | null;
  reason?: string | null;
  instructions?: string | null;
  action_count: number;
  ws_connected: boolean;
  ws_token?: string;
}

export interface DesktopStatusResponse {
  enabled: boolean;
  available: boolean;
  driver: string;
  platform: string;
  supports_control: boolean;
  supports_clipboard: boolean;
  displays: DesktopDisplayInfo[];
  spaces: DesktopSpaceInfo[];
  supports_space_switch: boolean;
  permissions: DesktopPermissions;
  error?: string | null;
  driver_error?: string | null;
  active_session?: DesktopSession | null;
}

export interface DesktopTakeoverContext {
  runId?: string;
  topicId?: string;
  reason?: string;
  instructions?: string | null;
  url?: string | null;
}
