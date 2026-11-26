import type {
  AdapterInfo,
  Artifact,
  BatchReassignTopicPayload,
  BatchReassignTopicResponse,
  ChatRequest,
  ChatResponse,
  DirectoryBrowseResponse,
  GlobalWorkspace,
  HealthResponse,
  HitlRequest,
  KikiStatus,
  Message,
  MessagePage,
  RawLogLine,
  ReassignTopicPayload,
  ReassignTopicResponse,
  ResumeRunPayload,
  SessionRun,
  StepLog,
  Topic,
  TopicWorkspace,
  WorkspaceMount,
} from './types';

const API_BASE = '/api';

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data?.detail || data?.message || detail;
    } catch {
      // ignore json parse error
    }
    throw new Error(detail);
  }

  return res.json() as Promise<T>;
}

export async function fetchHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>('/health');
}

export async function fetchGlobalWorkspace(): Promise<GlobalWorkspace> {
  return requestJson<GlobalWorkspace>('/workspace');
}

export async function browseDirectories(
  path?: string,
  showHidden = false
): Promise<DirectoryBrowseResponse> {
  const params = new URLSearchParams();
  if (path) params.set('path', path);
  if (showHidden) params.set('show_hidden', 'true');
  const qs = params.toString();
  return requestJson<DirectoryBrowseResponse>(`/workspace/browse${qs ? `?${qs}` : ''}`);
}

export async function fetchTopicWorkspace(topicId: string): Promise<TopicWorkspace> {
  return requestJson<TopicWorkspace>(`/topics/${encodeURIComponent(topicId)}/workspace`);
}

export async function addWorkspaceMount(payload: {
  scope: 'global' | 'topic';
  topic_id?: string | null;
  path: string;
  label?: string;
  is_primary?: boolean;
}): Promise<WorkspaceMount> {
  return requestJson<WorkspaceMount>('/workspace/mounts', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function updateWorkspaceMount(
  mountId: string,
  payload: { label?: string; is_primary?: boolean }
): Promise<WorkspaceMount> {
  return requestJson<WorkspaceMount>(`/workspace/mounts/${encodeURIComponent(mountId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function deleteWorkspaceMount(mountId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/workspace/mounts/${encodeURIComponent(mountId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data?.detail || data?.message || detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }
}

export async function fetchAdapters(): Promise<AdapterInfo[]> {
  return requestJson<AdapterInfo[]>('/adapters');
}

export async function fetchTopics(): Promise<Topic[]> {
  const data = await requestJson<Topic[] | { topics: Topic[] }>('/topics');
  return Array.isArray(data) ? data : data.topics ?? [];
}

export async function createTopic(payload: {
  title: string;
  summary?: string;
}): Promise<Topic> {
  const data = await requestJson<Topic | { topic: Topic }>('/topics', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  return 'topic' in data ? data.topic : data;
}

export async function updateTopic(
  topicId: string,
  payload: Partial<Pick<Topic, 'title' | 'summary' | 'status' | 'unread'>>
): Promise<Topic> {
  const data = await requestJson<Topic | { topic: Topic }>(
    `/topics/${encodeURIComponent(topicId)}`,
    {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }
  );
  return 'topic' in data ? data.topic : data;
}

export interface FetchMessagesOptions {
  topicId?: string | null;
  beforeId?: string | null;
  limit?: number;
}

export async function fetchMessagesPage(
  options: FetchMessagesOptions = {}
): Promise<MessagePage> {
  const params = new URLSearchParams();
  if (options.topicId) {
    params.set('topic_id', options.topicId);
  }
  if (options.beforeId) {
    params.set('before_id', options.beforeId);
  }
  if (typeof options.limit === 'number') {
    params.set('limit', String(options.limit));
  }
  const qs = params.toString();
  const data = await requestJson<Message[] | MessagePage>(
    `/messages${qs ? `?${qs}` : ''}`
  );
  if (Array.isArray(data)) {
    return { messages: data, has_more: false, next_cursor: null };
  }
  return {
    messages: data.messages ?? [],
    has_more: Boolean(data.has_more),
    next_cursor: data.next_cursor ?? null,
  };
}

export async function fetchActiveRuns(): Promise<SessionRun[]> {
  return requestJson<SessionRun[]>('/runs?active_only=true');
}

export async function sendChat(payload: ChatRequest): Promise<ChatResponse> {
  return requestJson<ChatResponse>('/chat', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function reassignMessageTopic(
  messageId: string,
  payload: ReassignTopicPayload
): Promise<ReassignTopicResponse> {
  return requestJson<ReassignTopicResponse>(
    `/messages/${encodeURIComponent(messageId)}/topic`,
    {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }
  );
}

export async function batchReassignMessageTopics(
  payload: BatchReassignTopicPayload
): Promise<BatchReassignTopicResponse> {
  return requestJson<BatchReassignTopicResponse>('/messages/batch-topic', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function fetchRun(runId: string): Promise<SessionRun> {
  const data = await requestJson<SessionRun | { run: SessionRun }>(
    `/runs/${encodeURIComponent(runId)}`
  );
  return 'run' in data ? data.run : data;
}

export async function resumeRun(
  runId: string,
  payload: ResumeRunPayload
): Promise<SessionRun> {
  const data = await requestJson<SessionRun | { run: SessionRun }>(
    `/runs/${encodeURIComponent(runId)}/resume`,
    {
      method: 'POST',
      body: JSON.stringify(payload),
    }
  );
  return 'run' in data ? data.run : data;
}

export async function cancelRun(runId: string): Promise<SessionRun> {
  const data = await requestJson<SessionRun | { run: SessionRun }>(
    `/runs/${encodeURIComponent(runId)}/cancel`,
    {
      method: 'POST',
    }
  );
  return 'run' in data ? data.run : data;
}

export interface SseEventHandlers {
  onOpen?: () => void;
  onError?: () => void;
  onKikiStatus?: (payload: { status: KikiStatus; reason?: string }) => void;
  onTopicCreated?: (payload: { topic: Topic }) => void;
  onTopicUpdated?: (payload: { topic: Topic }) => void;
  onMessageCreated?: (payload: { message: Message }) => void;
  onMessageUpdated?: (payload: { message: Message }) => void;
  onMessageDelta?: (payload: {
    message_id: string;
    topic_id: string;
    delta: string;
  }) => void;
  onMessageCompleted?: (payload: { message: Message }) => void;
  onRunCreated?: (payload: { run: SessionRun }) => void;
  onRunUpdated?: (payload: { run: SessionRun }) => void;
  onStepLog?: (payload: { run_id: string; step_log: StepLog }) => void;
  onArtifactCreated?: (payload: { run_id: string; artifact: Artifact }) => void;
  onRawLog?: (payload: RawLogLine & { run_id: string }) => void;
  onHitlRequired?: (payload: HitlRequest & { run_id: string; topic_id: string }) => void;
  onToastNotification?: (payload: {
    topic_id: string;
    topic_title: string;
    run_id: string;
    summary: string;
  }) => void;
}

export function connectEventStream(handlers: SseEventHandlers): () => void {
  let eventSource: EventSource | null = null;
  let reconnectTimer: number | null = null;
  let closed = false;
  let retryDelay = 1200;

  const parseAndDispatch = <T>(
    ev: MessageEvent,
    callback?: (payload: T) => void
  ) => {
    if (!callback) return;
    try {
      const parsed = JSON.parse(ev.data) as T;
      callback(parsed);
    } catch {
      // Ignore malformed frames
    }
  };

  const connect = () => {
    if (closed) return;
    eventSource = new EventSource(`${API_BASE}/events`);

    eventSource.onopen = () => {
      retryDelay = 1200;
      handlers.onOpen?.();
    };

    eventSource.onerror = () => {
      handlers.onError?.();
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      if (!closed) {
        reconnectTimer = window.setTimeout(() => {
          retryDelay = Math.min(retryDelay * 1.5, 10000);
          connect();
        }, retryDelay);
      }
    };

    eventSource.addEventListener('kiki_status', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onKikiStatus)
    );
    eventSource.addEventListener('topic_created', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onTopicCreated)
    );
    eventSource.addEventListener('topic_updated', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onTopicUpdated)
    );
    eventSource.addEventListener('message_created', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onMessageCreated)
    );
    eventSource.addEventListener('message_updated', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onMessageUpdated)
    );
    eventSource.addEventListener('message_delta', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onMessageDelta)
    );
    eventSource.addEventListener('message_completed', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onMessageCompleted)
    );
    eventSource.addEventListener('run_created', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onRunCreated)
    );
    eventSource.addEventListener('run_updated', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onRunUpdated)
    );
    eventSource.addEventListener('step_log', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onStepLog)
    );
    eventSource.addEventListener('artifact_created', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onArtifactCreated)
    );
    eventSource.addEventListener('raw_log', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onRawLog)
    );
    eventSource.addEventListener('hitl_required', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onHitlRequired)
    );
    eventSource.addEventListener('toast_notification', (e) =>
      parseAndDispatch(e as MessageEvent, handlers.onToastNotification)
    );
  };

  connect();

  return () => {
    closed = true;
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer);
    }
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  };
}
