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
}

export interface Topic {
  id: string;
  title: string;
  summary?: string | null;
  status: TopicStatus;
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

export interface MessageMetadata {
  confidence?: number;
  reasoning?: string;
  path?: 'fast' | 'heavy';
  session_run_id?: string;
  streaming?: boolean;
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

export interface ToastNotification {
  id: string;
  topic_id: string;
  topic_title: string;
  run_id: string;
  summary: string;
  created_at: number;
}
