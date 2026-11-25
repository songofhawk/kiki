import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  Check,
  FolderInput,
  GitBranchPlus,
  Lock,
  Menu,
  Send,
  Sparkles,
  X,
  Zap,
  Cpu,
  Compass,
} from 'lucide-react';
import {
  batchReassignMessageTopics,
  cancelRun,
  connectEventStream,
  createTopic,
  fetchActiveRuns,
  fetchAdapters,
  fetchHealth,
  fetchMessagesPage,
  fetchRun,
  fetchTopics,
  reassignMessageTopic,
  resumeRun,
  sendChat,
  updateTopic,
} from './api';
import type {
  AdapterInfo,
  BatchReassignTopicPayload,
  HealthResponse,
  KikiStatus,
  Message,
  RawLogLine,
  ReassignTopicPayload,
  ResumeRunPayload,
  SessionRun,
  ToastNotification,
  Topic,
} from './types';
import Sidebar from './components/Sidebar';
import type { KikiPose } from './components/KikiAvatar';
import UnifiedMessageList from './components/UnifiedMessageList';
import LogDrawer from './components/LogDrawer';
import ToastContainer from './components/ToastContainer';

type PathOption = 'auto' | 'fast' | 'heavy';

const ALL_STREAM_KEY = '__all__';
const MESSAGE_PAGE_SIZE = 40;

interface StreamPageState {
  messages: Message[];
  hasMore: boolean;
  nextCursor: string | null;
  loaded: boolean;
  loadingInitial: boolean;
  loadingOlder: boolean;
}

const EMPTY_STREAM: StreamPageState = {
  messages: [],
  hasMore: false,
  nextCursor: null,
  loaded: false,
  loadingInitial: false,
  loadingOlder: false,
};

function upsertTopic(list: Topic[], incoming: Topic): Topic[] {
  const idx = list.findIndex((t) => t.id === incoming.id);
  const next =
    idx >= 0
      ? list.map((t, i) => (i === idx ? { ...t, ...incoming } : t))
      : [incoming, ...list];
  return next.sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''));
}

function isMessageStreaming(msg: Message | undefined | null): boolean {
  if (!msg) return false;
  if (typeof msg.streaming === 'boolean') return msg.streaming;
  return Boolean(msg.metadata?.streaming);
}

function normalizeMessage(msg: Message): Message {
  const streaming = isMessageStreaming(msg);
  return msg.streaming === streaming ? msg : { ...msg, streaming };
}

function mergeMessage(existing: Message, incomingRaw: Message): Message {
  const incoming = normalizeMessage(incomingRaw);
  const existingStreaming = isMessageStreaming(existing);
  const incomingStreaming = isMessageStreaming(incoming);
  const incomingIsEmptyPlaceholder =
    incomingStreaming && !(incoming.content && incoming.content.length > 0);

  // Never let a stale empty streaming placeholder (e.g. from /api/chat HTTP response
  // or late message_created) wipe out already-streamed deltas or completed content.
  if (incomingIsEmptyPlaceholder && existing.content && existing.content.length > 0) {
    const alreadyCompleted =
      !existingStreaming && existing.metadata?.streaming === false;
    return {
      ...existing,
      topic: incoming.topic ?? existing.topic,
      session_run: incoming.session_run ?? existing.session_run,
      streaming: alreadyCompleted ? false : true,
    };
  }

  // If existing is already completed and incoming is still marked streaming (out-of-order),
  // keep completed content & state.
  const existingCompleted =
    !existingStreaming &&
    existing.metadata?.streaming === false &&
    Boolean(existing.content);
  if (existingCompleted && incomingStreaming) {
    return {
      ...existing,
      topic: incoming.topic ?? existing.topic,
      session_run: incoming.session_run ?? existing.session_run,
      streaming: false,
    };
  }

  return {
    ...existing,
    ...incoming,
    streaming: incomingStreaming,
    session_run: incoming.session_run ?? existing.session_run,
    topic: incoming.topic ?? existing.topic,
  };
}

function upsertMessage(list: Message[], incomingRaw: Message): Message[] {
  const incoming = normalizeMessage(incomingRaw);
  const idx = list.findIndex((m) => m.id === incoming.id);
  if (idx >= 0) {
    return list.map((m, i) => (i === idx ? mergeMessage(m, incoming) : m));
  }
  return [...list, incoming];
}

function upsertMessageChronological(list: Message[], incomingRaw: Message): Message[] {
  const incoming = normalizeMessage(incomingRaw);
  const idx = list.findIndex((m) => m.id === incoming.id);
  if (idx >= 0) {
    return list.map((m, i) => (i === idx ? mergeMessage(m, incoming) : m));
  }
  const incomingTs = incoming.created_at || '';
  const insertAt = list.findIndex((m) => (m.created_at || '') > incomingTs);
  if (insertAt < 0) {
    return [...list, incoming];
  }
  const next = list.slice();
  next.splice(insertAt, 0, incoming);
  return next;
}

function replaceOptimisticOrUpsert(
  list: Message[],
  tempId: string | undefined,
  incoming: Message
): Message[] {
  if (!tempId) {
    return upsertMessage(list, incoming);
  }
  const hasReal = list.some((m) => m.id === incoming.id);
  if (hasReal) {
    return upsertMessage(
      list.filter((m) => m.id !== tempId),
      incoming
    );
  }
  const tempIdx = list.findIndex((m) => m.id === tempId);
  if (tempIdx >= 0) {
    return list.map((m, i) =>
      i === tempIdx
        ? {
          ...incoming,
          session_run: incoming.session_run ?? m.session_run,
          topic: incoming.topic ?? m.topic,
        }
        : m
    );
  }
  return [...list, incoming];
}

function applyMessageCreatedToStreams(
  prev: Record<string, StreamPageState>,
  incoming: Message,
  tempId?: string
): Record<string, StreamPageState> {
  const next: Record<string, StreamPageState> = { ...prev };
  const targetKeys = new Set<string>([ALL_STREAM_KEY, incoming.topic_id]);

  for (const [key, stream] of Object.entries(prev)) {
    if (targetKeys.has(key)) {
      if (stream.loaded || stream.messages.length > 0) {
        next[key] = {
          ...stream,
          messages: replaceOptimisticOrUpsert(stream.messages, tempId, incoming),
        };
      }
    } else if (tempId && stream.messages.some((m) => m.id === tempId)) {
      next[key] = {
        ...stream,
        messages: stream.messages.filter((m) => m.id !== tempId),
      };
    }
  }
  return next;
}

function applyMessageUpdatedToStreams(
  prev: Record<string, StreamPageState>,
  incoming: Message
): Record<string, StreamPageState> {
  let changed = false;
  const next: Record<string, StreamPageState> = { ...prev };

  for (const [key, stream] of Object.entries(prev)) {
    if (!stream.loaded && stream.messages.length === 0) continue;

    if (key === ALL_STREAM_KEY) {
      if (stream.messages.some((m) => m.id === incoming.id)) {
        next[key] = {
          ...stream,
          messages: upsertMessage(stream.messages, incoming),
        };
        changed = true;
      }
      continue;
    }

    if (key === incoming.topic_id) {
      const exists = stream.messages.some((m) => m.id === incoming.id);
      if (exists) {
        next[key] = {
          ...stream,
          messages: upsertMessage(stream.messages, incoming),
        };
        changed = true;
      } else {
        const oldestTs = stream.messages[0]?.created_at || '';
        const incomingTs = incoming.created_at || '';
        if (!stream.hasMore || stream.messages.length === 0 || incomingTs >= oldestTs) {
          next[key] = {
            ...stream,
            messages: upsertMessageChronological(stream.messages, incoming),
          };
          changed = true;
        }
      }
    } else if (stream.messages.some((m) => m.id === incoming.id)) {
      next[key] = {
        ...stream,
        messages: stream.messages.filter((m) => m.id !== incoming.id),
      };
      changed = true;
    }
  }

  return changed ? next : prev;
}

function applyMessageDeltaToStreams(
  prev: Record<string, StreamPageState>,
  messageId: string,
  topicId: string,
  delta: string
): Record<string, StreamPageState> {
  const next: Record<string, StreamPageState> = { ...prev };
  const targetKeys = [ALL_STREAM_KEY, topicId];
  let changed = false;

  for (const key of targetKeys) {
    const stream = prev[key];
    if (!stream || (!stream.loaded && stream.messages.length === 0)) continue;
    const exists = stream.messages.some((m) => m.id === messageId);
    const updatedMessages = exists
      ? stream.messages.map((m) =>
        m.id === messageId
          ? { ...m, content: (m.content || '') + delta, streaming: true }
          : m
      )
      : [
        ...stream.messages,
        {
          id: messageId,
          topic_id: topicId,
          role: 'assistant' as const,
          content: delta,
          created_at: new Date().toISOString(),
          streaming: true,
        },
      ];
    next[key] = { ...stream, messages: updatedMessages };
    changed = true;
  }

  return changed ? next : prev;
}

function mergeRun(prev: SessionRun | undefined, incoming: SessionRun): SessionRun {
  if (!prev) return incoming;
  const incomingIsOlder =
    Boolean(incoming.updated_at && prev.updated_at) &&
    incoming.updated_at < prev.updated_at;
  const primary = incomingIsOlder ? prev : incoming;
  const secondary = incomingIsOlder ? incoming : prev;
  return {
    ...secondary,
    ...primary,
    step_logs:
      primary.step_logs && primary.step_logs.length > 0
        ? primary.step_logs
        : secondary.step_logs,
    artifacts:
      primary.artifacts && primary.artifacts.length > 0
        ? primary.artifacts
        : secondary.artifacts,
    raw_logs:
      primary.raw_logs && primary.raw_logs.length > 0
        ? primary.raw_logs
        : secondary.raw_logs,
    hitl_request:
      primary.status === 'waiting_input'
        ? primary.hitl_request ?? secondary.hitl_request
        : primary.hitl_request ?? null,
  };
}

export const App: React.FC = () => {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [adapters, setAdapters] = useState<AdapterInfo[]>([]);
  const [selectedAgentType, setSelectedAgentType] = useState<string>('auto');
  const [topics, setTopics] = useState<Topic[]>([]);
  const [streamsByKey, setStreamsByKey] = useState<Record<string, StreamPageState>>({
    [ALL_STREAM_KEY]: { ...EMPTY_STREAM, loadingInitial: true },
  });
  const streamsByKeyRef = useRef<Record<string, StreamPageState>>(streamsByKey);
  streamsByKeyRef.current = streamsByKey;
  const [runsById, setRunsById] = useState<Record<string, SessionRun>>({});
  const [rawLogsByRunId, setRawLogsByRunId] = useState<Record<string, RawLogLine[]>>({});

  const [focusTopicId, setFocusTopicId] = useState<string | null>(null);
  const focusTopicIdRef = useRef<string | null>(null);
  focusTopicIdRef.current = focusTopicId;

  const [serverKikiStatus, setServerKikiStatus] = useState<KikiStatus>('idle');
  const [sseConnected, setSseConnected] = useState(false);
  const [pendingSendCount, setPendingSendCount] = useState(0);
  const [inputText, setInputText] = useState('');
  const [pathOption, setPathOption] = useState<PathOption>('auto');
  const [errorBanner, setErrorBanner] = useState<string | null>(null);

  const [activeDrawerRunId, setActiveDrawerRunId] = useState<string | null>(null);
  const [toasts, setToasts] = useState<ToastNotification[]>([]);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedMessageIds, setSelectedMessageIds] = useState<Set<string>>(new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchMoveOpen, setBatchMoveOpen] = useState(false);
  const [batchSplitOpen, setBatchSplitOpen] = useState(false);
  const [batchSplitTitle, setBatchSplitTitle] = useState('');

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const lastSelectedMsgIdRef = useRef<string | null>(null);
  const pendingOptimisticRef = useRef<
    Array<{ tempId: string; content: string; focusTopicId: string | null }>
  >([]);
  const resolvedOptimisticMapRef = useRef<Map<string, string>>(new Map());
  const loadingInitialKeysRef = useRef<Set<string>>(new Set());
  const loadingOlderKeysRef = useRef<Set<string>>(new Set());

  const hydrateRunsFromMessages = useCallback((msgs: Message[]) => {
    const found: SessionRun[] = [];
    for (const msg of msgs) {
      if (msg.session_run) {
        found.push(msg.session_run);
      }
    }
    if (found.length === 0) return;
    setRunsById((prev) => {
      const next = { ...prev };
      for (const run of found) {
        next[run.id] = mergeRun(prev[run.id], run);
      }
      return next;
    });
  }, []);

  const loadStreamFirstPage = useCallback(
    async (topicId: string | null) => {
      const viewKey = topicId ?? ALL_STREAM_KEY;
      if (loadingInitialKeysRef.current.has(viewKey)) return;
      loadingInitialKeysRef.current.add(viewKey);

      setStreamsByKey((prev) => ({
        ...prev,
        [viewKey]: {
          ...(prev[viewKey] ?? EMPTY_STREAM),
          loadingInitial: true,
        },
      }));

      try {
        const page = await fetchMessagesPage({
          topicId,
          limit: MESSAGE_PAGE_SIZE,
        });
        hydrateRunsFromMessages(page.messages);
        setStreamsByKey((prev) => {
          const current = prev[viewKey];
          const existingById = new Map<string, Message>(
            (current?.messages ?? []).map((m) => [m.id, m])
          );
          let merged = page.messages.map((fetched) => {
            const existing = existingById.get(fetched.id);
            return existing ? mergeMessage(existing, fetched) : normalizeMessage(fetched);
          });
          if (current && current.messages.length > 0) {
            const fetchedIds = new Set(page.messages.map((m) => m.id));
            const liveTail = current.messages.filter(
              (m) => !fetchedIds.has(m.id) && (isMessageStreaming(m) || m.metadata?.optimistic)
            );
            if (liveTail.length > 0) {
              merged = [...merged, ...liveTail];
            }
          }
          return {
            ...prev,
            [viewKey]: {
              messages: merged,
              hasMore: page.has_more,
              nextCursor: page.next_cursor,
              loaded: true,
              loadingInitial: false,
              loadingOlder: false,
            },
          };
        });
      } catch (err) {
        setStreamsByKey((prev) => ({
          ...prev,
          [viewKey]: {
            ...(prev[viewKey] ?? EMPTY_STREAM),
            loadingInitial: false,
          },
        }));
        setErrorBanner(
          err instanceof Error ? err.message : '加载消息历史失败，请检查后端服务是否已启动'
        );
      } finally {
        loadingInitialKeysRef.current.delete(viewKey);
      }
    },
    [hydrateRunsFromMessages]
  );

  const handleLoadOlder = useCallback(async () => {
    const topicId = focusTopicIdRef.current;
    const viewKey = topicId ?? ALL_STREAM_KEY;
    const current = streamsByKeyRef.current[viewKey];
    if (
      !current ||
      !current.loaded ||
      !current.hasMore ||
      !current.nextCursor ||
      loadingOlderKeysRef.current.has(viewKey)
    ) {
      return;
    }

    const cursor = current.nextCursor;
    loadingOlderKeysRef.current.add(viewKey);
    setStreamsByKey((prev) => {
      const existing = prev[viewKey];
      if (!existing) return prev;
      return {
        ...prev,
        [viewKey]: { ...existing, loadingOlder: true },
      };
    });

    try {
      const page = await fetchMessagesPage({
        topicId,
        beforeId: cursor,
        limit: MESSAGE_PAGE_SIZE,
      });
      hydrateRunsFromMessages(page.messages);
      setStreamsByKey((prev) => {
        const existing = prev[viewKey];
        if (!existing) return prev;
        const existingIds = new Set(existing.messages.map((m) => m.id));
        const uniqueOlder = page.messages.filter((m) => !existingIds.has(m.id));
        return {
          ...prev,
          [viewKey]: {
            ...existing,
            messages: [...uniqueOlder, ...existing.messages],
            hasMore: page.has_more,
            nextCursor: page.next_cursor,
            loadingOlder: false,
          },
        };
      });
    } catch (err) {
      setStreamsByKey((prev) => {
        const existing = prev[viewKey];
        if (!existing) return prev;
        return {
          ...prev,
          [viewKey]: { ...existing, loadingOlder: false },
        };
      });
      setErrorBanner(
        err instanceof Error ? err.message : '加载更早的消息失败，请重试'
      );
    } finally {
      loadingOlderKeysRef.current.delete(viewKey);
    }
  }, [hydrateRunsFromMessages]);

  const pushToast = useCallback(
    (payload: {
      topic_id: string;
      topic_title: string;
      run_id: string;
      summary: string;
    }) => {
      const id = `${payload.run_id}-${Date.now()}`;
      const toast: ToastNotification = {
        id,
        ...payload,
        created_at: Date.now(),
      };
      setToasts((prev) => [toast, ...prev].slice(0, 4));
      window.setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, 3500);
    },
    []
  );

  const dismissToast = useCallback((toastId: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== toastId));
  }, []);

  // Hydrate initial state (latest page of All Stream + active runs)
  const loadInitialData = useCallback(async () => {
    loadingInitialKeysRef.current.add(ALL_STREAM_KEY);
    try {
      const [healthRes, adaptersRes, topicsRes, activeRunsRes, messagesPage] =
        await Promise.all([
          fetchHealth().catch(() => null),
          fetchAdapters().catch(() => []),
          fetchTopics(),
          fetchActiveRuns().catch(() => []),
          fetchMessagesPage({ limit: MESSAGE_PAGE_SIZE }),
        ]);
      if (healthRes) setHealth(healthRes);
      if (adaptersRes.length > 0) setAdapters(adaptersRes);
      setTopics(topicsRes);

      setStreamsByKey((prev) => {
        const current = prev[ALL_STREAM_KEY];
        const existingById = new Map<string, Message>(
          (current?.messages ?? []).map((m) => [m.id, m])
        );
        let merged = messagesPage.messages.map((fetched) => {
          const existing = existingById.get(fetched.id);
          return existing ? mergeMessage(existing, fetched) : normalizeMessage(fetched);
        });
        if (current && current.messages.length > 0) {
          const fetchedIds = new Set(messagesPage.messages.map((m) => m.id));
          const liveTail = current.messages.filter(
            (m) => !fetchedIds.has(m.id) && (isMessageStreaming(m) || m.metadata?.optimistic)
          );
          if (liveTail.length > 0) {
            merged = [...merged, ...liveTail];
          }
        }
        return {
          ...prev,
          [ALL_STREAM_KEY]: {
            messages: merged,
            hasMore: messagesPage.has_more,
            nextCursor: messagesPage.next_cursor,
            loaded: true,
            loadingInitial: false,
            loadingOlder: false,
          },
        };
      });

      setRunsById((prev) => {
        const next = { ...prev };
        for (const msg of messagesPage.messages) {
          if (msg.session_run) {
            next[msg.session_run.id] = mergeRun(next[msg.session_run.id], msg.session_run);
          }
        }
        for (const run of activeRunsRes) {
          next[run.id] = mergeRun(next[run.id], run);
        }
        return next;
      });
    } catch (err) {
      setStreamsByKey((prev) => ({
        ...prev,
        [ALL_STREAM_KEY]: {
          ...(prev[ALL_STREAM_KEY] ?? EMPTY_STREAM),
          loadingInitial: false,
        },
      }));
      setErrorBanner(
        err instanceof Error ? err.message : '加载初始数据失败，请检查后端服务是否已启动'
      );
    } finally {
      loadingInitialKeysRef.current.delete(ALL_STREAM_KEY);
    }
  }, []);

  useEffect(() => {
    loadInitialData();
  }, [loadInitialData]);

  // Subscribe to global SSE event stream
  useEffect(() => {
    let wasDisconnected = false;
    const disconnect = connectEventStream({
      onOpen: () => {
        setSseConnected(true);
        setErrorBanner(null);
        if (wasDisconnected) {
          wasDisconnected = false;
          fetchTopics()
            .then((list) => setTopics(list))
            .catch(() => { });
          fetchActiveRuns()
            .then((runs) => {
              setRunsById((prev) => {
                const next = { ...prev };
                for (const run of runs) {
                  next[run.id] = mergeRun(next[run.id], run);
                }
                return next;
              });
            })
            .catch(() => { });
          void loadStreamFirstPage(focusTopicIdRef.current);
        }
      },
      onError: () => {
        wasDisconnected = true;
        setSseConnected(false);
      },
      onKikiStatus: ({ status }) => {
        setServerKikiStatus(status);
      },
      onTopicCreated: ({ topic }) => {
        setTopics((prev) => upsertTopic(prev, topic));
      },
      onTopicUpdated: ({ topic }) => {
        setTopics((prev) => upsertTopic(prev, topic));
      },
      onMessageCreated: ({ message }) => {
        if (message.role === 'user') {
          let matchIdx = pendingOptimisticRef.current.findIndex(
            (item) =>
              item.content === message.content &&
              item.focusTopicId === message.topic_id
          );
          if (matchIdx < 0) {
            matchIdx = pendingOptimisticRef.current.findIndex(
              (item) =>
                item.content === message.content && item.focusTopicId === null
            );
          }
          if (matchIdx < 0) {
            matchIdx = pendingOptimisticRef.current.findIndex(
              (item) => item.content === message.content
            );
          }
          if (matchIdx >= 0) {
            const [matched] = pendingOptimisticRef.current.splice(matchIdx, 1);
            resolvedOptimisticMapRef.current.set(matched.tempId, message.id);
            setStreamsByKey((prev) =>
              applyMessageCreatedToStreams(prev, message, matched.tempId)
            );
          } else {
            setStreamsByKey((prev) => applyMessageCreatedToStreams(prev, message));
          }
        } else {
          setStreamsByKey((prev) => applyMessageCreatedToStreams(prev, message));
        }
        if (message.session_run) {
          setRunsById((prev) => ({
            ...prev,
            [message.session_run!.id]: mergeRun(
              prev[message.session_run!.id],
              message.session_run!
            ),
          }));
        }
      },
      onMessageDelta: ({ message_id, topic_id, delta }) => {
        setStreamsByKey((prev) =>
          applyMessageDeltaToStreams(prev, message_id, topic_id, delta)
        );
      },
      onMessageCompleted: ({ message }) => {
        setStreamsByKey((prev) =>
          applyMessageCreatedToStreams(prev, { ...message, streaming: false })
        );
      },
      onMessageUpdated: ({ message }) => {
        setStreamsByKey((prev) => applyMessageUpdatedToStreams(prev, message));
      },
      onRunCreated: ({ run }) => {
        setRunsById((prev) => ({
          ...prev,
          [run.id]: mergeRun(prev[run.id], run),
        }));
        // Ensure trigger message points to this run across all loaded streams
        if (run.trigger_message_id) {
          setStreamsByKey((prev) => {
            let changed = false;
            const next: Record<string, StreamPageState> = { ...prev };
            for (const [key, stream] of Object.entries(prev)) {
              if (
                stream.messages.some(
                  (m) => m.id === run.trigger_message_id && !m.session_run
                )
              ) {
                next[key] = {
                  ...stream,
                  messages: stream.messages.map((m) =>
                    m.id === run.trigger_message_id && !m.session_run
                      ? { ...m, session_run: run }
                      : m
                  ),
                };
                changed = true;
              }
            }
            return changed ? next : prev;
          });
        }
        fetchTopics()
          .then((list) => setTopics(list))
          .catch(() => { });
      },
      onRunUpdated: ({ run }) => {
        setRunsById((prev) => ({
          ...prev,
          [run.id]: mergeRun(prev[run.id], run),
        }));
        if (
          run.status === 'completed' ||
          run.status === 'failed' ||
          run.status === 'cancelled'
        ) {
          fetchTopics()
            .then((list) => setTopics(list))
            .catch(() => { });
        }
      },
      onStepLog: ({ run_id, step_log }) => {
        setRunsById((prev) => {
          const existing = prev[run_id];
          if (!existing) return prev;
          const logs = existing.step_logs ?? [];
          const idx = logs.findIndex((s) => s.id === step_log.id);
          const nextLogs =
            idx >= 0
              ? logs.map((s, i) => (i === idx ? step_log : s))
              : [...logs, step_log];
          return {
            ...prev,
            [run_id]: {
              ...existing,
              step_logs: nextLogs,
            },
          };
        });
      },
      onArtifactCreated: ({ run_id, artifact }) => {
        setRunsById((prev) => {
          const existing = prev[run_id];
          if (!existing) return prev;
          const arts = existing.artifacts ?? [];
          if (arts.some((a) => a.id === artifact.id)) return prev;
          return {
            ...prev,
            [run_id]: {
              ...existing,
              artifacts: [...arts, artifact],
            },
          };
        });
      },
      onRawLog: (payload) => {
        const { run_id, ...line } = payload;
        setRawLogsByRunId((prev) => {
          const list = prev[run_id] ?? [];
          const next = [...list, line];
          return {
            ...prev,
            [run_id]: next.length > 1000 ? next.slice(-1000) : next,
          };
        });
      },
      onHitlRequired: (payload) => {
        setRunsById((prev) => {
          const existing = prev[payload.run_id];
          if (!existing) return prev;
          return {
            ...prev,
            [payload.run_id]: {
              ...existing,
              status: 'waiting_input',
              hitl_request: payload,
            },
          };
        });
      },
      onToastNotification: (payload) => {
        pushToast(payload);
        if (focusTopicIdRef.current !== payload.topic_id) {
          setTopics((prev) =>
            prev.map((t) =>
              t.id === payload.topic_id ? { ...t, unread: true } : t
            )
          );
        }
      },
    });

    return disconnect;
  }, [pushToast, loadStreamFirstPage]);

  const topicsById = useMemo(() => {
    const map: Record<string, Topic> = {};
    for (const t of topics) {
      map[t.id] = t;
    }
    return map;
  }, [topics]);

  const currentViewKey = focusTopicId ?? ALL_STREAM_KEY;
  const currentStream = streamsByKey[currentViewKey] ?? EMPTY_STREAM;
  const visibleMessages = currentStream.messages;

  const focusedTopic = focusTopicId ? topicsById[focusTopicId] ?? null : null;

  const hasStreamingMessage = useMemo(
    () =>
      Object.values(streamsByKey).some((stream) =>
        stream.messages.some((m) => isMessageStreaming(m))
      ),
    [streamsByKey]
  );

  const hasPendingSends = pendingSendCount > 0;

  const activeRunCount = useMemo(
    () =>
      Object.values(runsById).filter(
        (r) =>
          r.status === 'queued' ||
          r.status === 'running' ||
          r.status === 'waiting_input'
      ).length,
    [runsById]
  );

  const globalKikiStatus: KikiStatus = useMemo(() => {
    const allRuns = Object.values(runsById);
    if (allRuns.some((r) => r.status === 'waiting_input')) {
      return 'waiting_input';
    }
    if (hasPendingSends || hasStreamingMessage || serverKikiStatus === 'thinking') {
      return 'thinking';
    }
    if (allRuns.some((r) => r.status === 'running' || r.status === 'queued')) {
      return 'running';
    }
    return serverKikiStatus;
  }, [runsById, hasPendingSends, hasStreamingMessage, serverKikiStatus]);

  // Compute which Kiki animations are currently visible in the main stream / toast
  // so the bottom-left companion Kiki NEVER performs the exact same animation at the same time.
  const { companionPose, companionCaption } = useMemo((): {
    companionPose: KikiPose | undefined;
    companionCaption: string | null;
  } => {
    const usedInMainView = new Set<KikiPose>();

    if (hasPendingSends || toasts.length > 0) {
      usedInMainView.add('delivering');
    }
    if (visibleMessages.some((m) => isMessageStreaming(m))) {
      usedInMainView.add('thinking');
    }

    const seenRunIds = new Set<string>();
    for (const msg of visibleMessages) {
      const rId =
        msg.session_run?.id ??
        (typeof msg.metadata?.session_run_id === 'string'
          ? msg.metadata.session_run_id
          : undefined);
      const run = rId ? runsById[rId] ?? msg.session_run ?? null : msg.session_run ?? null;
      if (!run || seenRunIds.has(run.id)) continue;
      seenRunIds.add(run.id);
      if (run.status === 'queued') usedInMainView.add('delivering');
      if (run.status === 'running') usedInMainView.add('working');
      if (run.status === 'waiting_input') usedInMainView.add('waiting_input');
    }

    const pickDistinct = (candidates: KikiPose[]): KikiPose =>
      candidates.find((p) => !usedInMainView.has(p)) ?? 'idle';

    switch (globalKikiStatus) {
      case 'waiting_input': {
        if (!usedInMainView.has('waiting_input')) {
          return { companionPose: 'waiting_input', companionCaption: '其他话题等你拍板！' };
        }
        return {
          companionPose: pickDistinct(['thinking', 'idle']),
          companionCaption: '等你拍板确认中...',
        };
      }
      case 'thinking': {
        if (hasPendingSends) {
          return {
            companionPose: pickDistinct(['thinking', 'idle']),
            companionCaption:
              pendingSendCount > 1
                ? `并行分流 ${pendingSendCount} 条消息中...`
                : '歪头判断话题分流中...',
          };
        }
        if (usedInMainView.has('thinking')) {
          return {
            companionPose: 'idle',
            companionCaption: '前台轻聊回复中...',
          };
        }
        return { companionPose: 'thinking', companionCaption: '歪头思考分流中...' };
      }
      case 'running': {
        if (!usedInMainView.has('working')) {
          return {
            companionPose: 'working',
            companionCaption:
              activeRunCount > 1
                ? `后台并行处理 ${activeRunCount} 个任务...`
                : '后台敲键盘干活中...',
          };
        }
        return {
          companionPose: pickDistinct(['delivering', 'thinking', 'idle']),
          companionCaption:
            activeRunCount > 1
              ? `后台并行盯梢 ${activeRunCount} 个任务...`
              : '后台跑腿盯梢中...',
        };
      }
      case 'idle':
      default:
        return { companionPose: undefined, companionCaption: null };
    }
  }, [
    visibleMessages,
    runsById,
    hasPendingSends,
    pendingSendCount,
    activeRunCount,
    toasts.length,
    globalKikiStatus,
  ]);

  const handleSelectTopic = useCallback(
    async (topicId: string) => {
      setFocusTopicId(topicId);
      setMobileSidebarOpen(false);
      const target = topics.find((t) => t.id === topicId);
      if (target?.unread) {
        setTopics((prev) =>
          prev.map((t) => (t.id === topicId ? { ...t, unread: false } : t))
        );
        updateTopic(topicId, { unread: false }).catch(() => { });
      }
      if (!streamsByKeyRef.current[topicId]?.loaded) {
        void loadStreamFirstPage(topicId);
      }
    },
    [topics, loadStreamFirstPage]
  );

  const handleSelectAllStream = useCallback(() => {
    setFocusTopicId(null);
    setMobileSidebarOpen(false);
    if (!streamsByKeyRef.current[ALL_STREAM_KEY]?.loaded) {
      void loadStreamFirstPage(null);
    }
  }, [loadStreamFirstPage]);

  const handleCreateTopic = useCallback(async (title: string) => {
    try {
      const created = await createTopic({ title });
      setTopics((prev) => upsertTopic(prev, created));
      setStreamsByKey((prev) => ({
        ...prev,
        [created.id]: {
          messages: [],
          hasMore: false,
          nextCursor: null,
          loaded: true,
          loadingInitial: false,
          loadingOlder: false,
        },
      }));
      setFocusTopicId(created.id);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : '创建话题失败');
    }
  }, []);

  const handleSend = useCallback(
    async (overrideText?: string) => {
      const content = (overrideText ?? inputText).trim();
      if (!content) return;

      const currentFocusTopicId = focusTopicId;
      const currentPathOption = pathOption;
      const currentAgentType = selectedAgentType;

      setPendingSendCount((count) => count + 1);
      setErrorBanner(null);
      if (!overrideText) {
        setInputText('');
        if (textareaRef.current) {
          textareaRef.current.style.height = 'auto';
        }
      }

      const tempId = `temp-user-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      const optimisticUser: Message = {
        id: tempId,
        topic_id: currentFocusTopicId ?? '__routing__',
        role: 'user',
        content,
        created_at: new Date().toISOString(),
        metadata: {
          optimistic: true,
          routing: !currentFocusTopicId,
        },
        topic: currentFocusTopicId ? topicsById[currentFocusTopicId] ?? null : null,
      };
      pendingOptimisticRef.current.push({
        tempId,
        content,
        focusTopicId: currentFocusTopicId,
      });
      setStreamsByKey((prev) => {
        const next: Record<string, StreamPageState> = { ...prev };
        const activeKey = currentFocusTopicId ?? ALL_STREAM_KEY;
        const activeStream = prev[activeKey] ?? { ...EMPTY_STREAM, loaded: true };
        next[activeKey] = {
          ...activeStream,
          messages: [...activeStream.messages, optimisticUser],
        };
        if (activeKey !== ALL_STREAM_KEY && prev[ALL_STREAM_KEY]?.loaded) {
          next[ALL_STREAM_KEY] = {
            ...prev[ALL_STREAM_KEY],
            messages: [...prev[ALL_STREAM_KEY].messages, optimisticUser],
          };
        }
        return next;
      });

      try {
        const res = await sendChat({
          content,
          focus_topic_id: currentFocusTopicId,
          force_path: currentPathOption === 'auto' ? null : currentPathOption,
          agent_type: currentAgentType === 'auto' ? null : currentAgentType,
        });

        if (res.topic) {
          setTopics((prev) => upsertTopic(prev, res.topic));
        }
        if (res.reassigned_messages && res.reassigned_messages.length > 0) {
          setStreamsByKey((prev) =>
            res.reassigned_messages!.reduce(
              (acc, msg) =>
                applyMessageUpdatedToStreams(acc, {
                  ...msg,
                  topic: res.topic ?? msg.topic,
                }),
              prev
            )
          );
        }
        if (res.user_message) {
          pendingOptimisticRef.current = pendingOptimisticRef.current.filter(
            (item) => item.tempId !== tempId
          );
          resolvedOptimisticMapRef.current.delete(tempId);
          const enrichedUser: Message = {
            ...res.user_message,
            topic: res.topic ?? res.user_message.topic,
            session_run:
              !res.assistant_message && res.session_run
                ? res.session_run
                : res.user_message.session_run,
          };
          setStreamsByKey((prev) =>
            applyMessageCreatedToStreams(prev, enrichedUser, tempId)
          );
        }
        if (res.assistant_message) {
          const enrichedAssistant: Message = {
            ...res.assistant_message,
            topic: res.topic ?? res.assistant_message.topic,
            session_run: res.session_run ?? res.assistant_message.session_run,
          };
          setStreamsByKey((prev) =>
            applyMessageCreatedToStreams(prev, enrichedAssistant)
          );
        }
        if (res.session_run) {
          setRunsById((prev) => ({
            ...prev,
            [res.session_run!.id]: mergeRun(
              prev[res.session_run!.id],
              res.session_run!
            ),
          }));
        }
      } catch (err) {
        pendingOptimisticRef.current = pendingOptimisticRef.current.filter(
          (item) => item.tempId !== tempId
        );
        resolvedOptimisticMapRef.current.delete(tempId);
        setStreamsByKey((prev) => {
          const next: Record<string, StreamPageState> = {};
          for (const [key, stream] of Object.entries(prev)) {
            next[key] = {
              ...stream,
              messages: stream.messages.filter((m) => m.id !== tempId),
            };
          }
          return next;
        });
        setErrorBanner(
          err instanceof Error ? err.message : '发送消息失败，请稍后重试'
        );
        if (!overrideText) {
          setInputText((prev) => (prev.trim() ? prev : content));
        }
      } finally {
        setPendingSendCount((count) => Math.max(0, count - 1));
      }
    },
    [inputText, focusTopicId, pathOption, selectedAgentType, topicsById]
  );

  const handleReassignMessage = useCallback(
    async (messageId: string, payload: ReassignTopicPayload) => {
      try {
        const res = await reassignMessageTopic(messageId, payload);
        if (res.topic) {
          setTopics((prev) => upsertTopic(prev, res.topic));
        }
        if (res.message) {
          setStreamsByKey((prev) =>
            applyMessageUpdatedToStreams(prev, {
              ...res.message,
              topic: res.topic ?? res.message.topic,
            })
          );
        }
        // Refresh topics to keep counts accurate
        fetchTopics()
          .then((list) => setTopics(list))
          .catch(() => { });
      } catch (err) {
        setErrorBanner(
          err instanceof Error ? err.message : '调整话题归属失败'
        );
      }
    },
    []
  );

  const exitSelectionMode = useCallback(() => {
    setSelectionMode(false);
    setSelectedMessageIds(new Set());
    setBatchMoveOpen(false);
    setBatchSplitOpen(false);
    setBatchSplitTitle('');
    lastSelectedMsgIdRef.current = null;
  }, []);

  useEffect(() => {
    if (!selectionMode) return;
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        exitSelectionMode();
      }
    };
    window.addEventListener('keydown', handleEsc);
    return () => window.removeEventListener('keydown', handleEsc);
  }, [selectionMode, exitSelectionMode]);

  const handleStartMultiSelect = useCallback((messageId: string) => {
    setSelectionMode(true);
    setSelectedMessageIds(new Set([messageId]));
    setBatchMoveOpen(false);
    setBatchSplitOpen(false);
    lastSelectedMsgIdRef.current = messageId;
  }, []);

  const handleToggleSelectMessage = useCallback(
    (messageId: string, shiftKey = false) => {
      setSelectedMessageIds((prev) => {
        const next = new Set(prev);
        if (shiftKey && lastSelectedMsgIdRef.current) {
          const selectable = visibleMessages.filter(
            (m) => m.role !== 'system' && !m.metadata?.optimistic
          );
          const startIdx = selectable.findIndex(
            (m) => m.id === lastSelectedMsgIdRef.current
          );
          const endIdx = selectable.findIndex((m) => m.id === messageId);
          if (startIdx >= 0 && endIdx >= 0) {
            const [lo, hi] = startIdx < endIdx ? [startIdx, endIdx] : [endIdx, startIdx];
            for (let i = lo; i <= hi; i += 1) {
              next.add(selectable[i].id);
            }
            return next;
          }
        }
        if (next.has(messageId)) {
          next.delete(messageId);
        } else {
          next.add(messageId);
        }
        return next;
      });
      lastSelectedMsgIdRef.current = messageId;
    },
    [visibleMessages]
  );

  const handleBatchReassign = useCallback(
    async (payload: Omit<BatchReassignTopicPayload, 'message_ids'>) => {
      const ids = Array.from(selectedMessageIds);
      if (ids.length === 0 || batchBusy) return;
      setBatchBusy(true);
      try {
        const res = await batchReassignMessageTopics({
          ...payload,
          message_ids: ids,
        });
        if (res.topic) {
          setTopics((prev) => upsertTopic(prev, res.topic));
        }
        if (res.messages?.length) {
          setStreamsByKey((prev) =>
            res.messages.reduce(
              (acc, msg) =>
                applyMessageUpdatedToStreams(acc, {
                  ...msg,
                  topic: res.topic ?? msg.topic,
                }),
              prev
            )
          );
        }
        fetchTopics()
          .then((list) => setTopics(list))
          .catch(() => { });
        exitSelectionMode();
      } catch (err) {
        setErrorBanner(
          err instanceof Error ? err.message : '批量调整话题归属失败'
        );
      } finally {
        setBatchBusy(false);
      }
    },
    [selectedMessageIds, batchBusy, exitSelectionMode]
  );

  const handleOpenLogDrawer = useCallback(async (runId: string) => {
    setActiveDrawerRunId(runId);
    try {
      const fullRun = await fetchRun(runId);
      setRunsById((prev) => ({
        ...prev,
        [fullRun.id]: mergeRun(prev[fullRun.id], fullRun),
      }));
      if (fullRun.raw_logs && fullRun.raw_logs.length > 0) {
        setRawLogsByRunId((prev) => ({
          ...prev,
          [fullRun.id]:
            (prev[fullRun.id]?.length ?? 0) >= fullRun.raw_logs!.length
              ? prev[fullRun.id]
              : fullRun.raw_logs!,
        }));
      }
    } catch {
      // Keep live SSE logs if fetchRun fails
    }
  }, []);

  const handleResumeRun = useCallback(
    async (runId: string, payload: ResumeRunPayload) => {
      try {
        const updated = await resumeRun(runId, payload);
        setRunsById((prev) => ({
          ...prev,
          [updated.id]: mergeRun(prev[updated.id], updated),
        }));
      } catch (err) {
        setErrorBanner(
          err instanceof Error ? err.message : '恢复任务失败，请重试'
        );
      }
    },
    []
  );

  const handleCancelRun = useCallback(async (runId: string) => {
    try {
      const updated = await cancelRun(runId);
      setRunsById((prev) => ({
        ...prev,
        [updated.id]: mergeRun(prev[updated.id], updated),
      }));
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : '取消任务失败');
    }
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleTextareaChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInputText(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  const activeDrawerRun = activeDrawerRunId
    ? runsById[activeDrawerRunId] ?? null
    : null;

  return (
    <div className="h-screen w-screen overflow-hidden flex bg-[#FAF8F5] text-[#262422]">
      {/* Desktop Sidebar */}
      <div className="hidden md:flex h-full">
        <Sidebar
          topics={topics}
          focusTopicId={focusTopicId}
          globalKikiStatus={globalKikiStatus}
          companionPose={companionPose}
          companionCaption={companionCaption}
          sseConnected={sseConnected}
          onSelectAllStream={handleSelectAllStream}
          onSelectTopic={handleSelectTopic}
          onCreateTopic={handleCreateTopic}
          onReassignMessage={handleReassignMessage}
        />
      </div>

      {/* Mobile Sidebar Drawer */}
      {mobileSidebarOpen && (
        <div className="fixed inset-0 z-40 flex md:hidden">
          <div
            onClick={() => setMobileSidebarOpen(false)}
            className="fixed inset-0 bg-black/25 backdrop-blur-[1px]"
          />
          <div className="relative z-10 h-full">
            <Sidebar
              topics={topics}
              focusTopicId={focusTopicId}
              globalKikiStatus={globalKikiStatus}
              companionPose={companionPose}
              companionCaption={companionCaption}
              sseConnected={sseConnected}
              onSelectAllStream={handleSelectAllStream}
              onSelectTopic={handleSelectTopic}
              onCreateTopic={handleCreateTopic}
              onReassignMessage={handleReassignMessage}
            />
          </div>
        </div>
      )}

      {/* Main Workspace */}
      <main className="flex-1 min-w-0 h-full flex flex-col kiki-paper-bg">
        {/* Dual-mode Top Header */}
        <header className="h-14 shrink-0 px-4 sm:px-6 bg-[#FAF8F5]/90 backdrop-blur-md border-b border-[#E8E2D9] flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5 min-w-0">
            <button
              type="button"
              onClick={() => setMobileSidebarOpen(true)}
              aria-label="打开侧边栏"
              className="md:hidden p-1.5 rounded-lg text-[#5A534C] hover:bg-[#EFEAE2]"
            >
              <Menu className="w-5 h-5" />
            </button>

            {focusTopicId && focusedTopic ? (
              <div className="flex items-center gap-2.5 min-w-0">
                <button
                  type="button"
                  onClick={handleSelectAllStream}
                  className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-medium text-[#5A534C] bg-white hover:bg-[#F3EFE9] border border-[#E5DED4] transition-colors shrink-0"
                >
                  <ArrowLeft className="w-3.5 h-3.5" />
                  <span>返回全局流</span>
                </button>
                <div className="min-w-0 flex items-center gap-2">
                  <span className="text-sm font-semibold text-[#262422] truncate">
                    Topic Focus · <span className="text-[#5B7A95]">#{focusedTopic.title}</span>
                  </span>
                  <span className="hidden sm:inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-[#EAF0F5] text-[#44637D] border border-[#CFE0EC] shrink-0">
                    <Lock className="w-3 h-3" />
                    锁定当前话题（绕过路由）
                  </span>
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-sm font-semibold text-[#262422] truncate">
                  All Stream · 全局流
                </span>
                <span className="hidden sm:inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] text-[#7D756C] bg-white border border-[#E8E2D9]">
                  <Sparkles className="w-3 h-3 text-[#5B7A95]" />
                  自动分流归类
                </span>
              </div>
            )}
          </div>

          <div className="flex items-center gap-2 shrink-0">
            {health?.model && (
              <span className="hidden sm:inline-block text-[11px] font-mono px-2 py-0.5 rounded-md bg-[#F2EDE6] text-[#6E665E] border border-[#E6DFD5]">
                {health.model}
              </span>
            )}
          </div>
        </header>

        {/* Error Banner */}
        {errorBanner && (
          <div className="px-4 sm:px-6 py-2 bg-rose-50 border-b border-rose-200 text-xs text-rose-800 flex items-center justify-between gap-3">
            <span className="truncate">{errorBanner}</span>
            <button
              type="button"
              onClick={() => setErrorBanner(null)}
              className="p-0.5 rounded hover:bg-rose-100 text-rose-600"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}

        {/* Message List */}
        <UnifiedMessageList
          messages={visibleMessages}
          topicsById={topicsById}
          allTopics={topics}
          runsById={runsById}
          focusTopicId={focusTopicId}
          hasMore={currentStream.hasMore}
          loadingInitial={currentStream.loadingInitial}
          loadingOlder={currentStream.loadingOlder}
          onLoadOlder={handleLoadOlder}
          sending={hasPendingSends}
          pendingSendCount={pendingSendCount}
          selectionMode={selectionMode}
          selectedMessageIds={selectedMessageIds}
          onToggleSelectMessage={handleToggleSelectMessage}
          onStartMultiSelect={handleStartMultiSelect}
          onSelectTopic={handleSelectTopic}
          onReassignMessage={handleReassignMessage}
          onOpenLogDrawer={handleOpenLogDrawer}
          onResumeRun={handleResumeRun}
          onCancelRun={handleCancelRun}
          onStarterPrompt={(prompt) => handleSend(prompt)}
        />

        {/* Bottom Composer or Hidden Multi-Select Floating Bar */}
        <div className="shrink-0 px-4 sm:px-6 lg:px-8 pb-4 pt-2 bg-gradient-to-t from-[#FAF8F5] via-[#FAF8F5] to-transparent">
          <div className="max-w-3xl mx-auto">
            {selectionMode ? (
              <div className="relative rounded-2xl bg-white border border-[#D8CFC2] shadow-lg shadow-stone-900/5 px-4 py-3 flex flex-wrap items-center justify-between gap-3 animate-kiki-fade-up">
                <div className="flex items-center gap-2.5 text-xs text-[#4E4842]">
                  <span className="inline-flex items-center justify-center px-2 py-0.5 rounded-full bg-[#EAF0F5] text-[#3B5972] font-semibold">
                    已选 {selectedMessageIds.size} 条
                  </span>
                  <span className="hidden sm:inline text-[#8C8378]">
                    点击气泡多选 · Shift 区间连选
                  </span>
                </div>

                {batchSplitOpen ? (
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      void handleBatchReassign({
                        action: 'split',
                        new_topic_title: batchSplitTitle.trim() || undefined,
                      });
                    }}
                    className="flex items-center gap-2 flex-1 justify-end min-w-[240px]"
                  >
                    <input
                      type="text"
                      autoFocus
                      value={batchSplitTitle}
                      onChange={(e) => setBatchSplitTitle(e.target.value)}
                      placeholder="新话题标题（留空自动命名）"
                      className="w-48 sm:w-56 px-2.5 py-1.5 text-xs rounded-lg border border-[#E8E2D9] bg-[#FAF8F5] focus:outline-none focus:border-[#5B7A95]"
                    />
                    <button
                      type="button"
                      onClick={() => {
                        setBatchSplitOpen(false);
                        setBatchSplitTitle('');
                      }}
                      className="px-2 py-1.5 text-xs text-[#7A7269] hover:text-[#262422]"
                    >
                      返回
                    </button>
                    <button
                      type="submit"
                      disabled={batchBusy || selectedMessageIds.size === 0}
                      className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium bg-[#5B7A95] text-white hover:bg-[#4A6882] disabled:opacity-40"
                    >
                      <Check className="w-3.5 h-3.5" />
                      确认拆分
                    </button>
                  </form>
                ) : (
                  <div className="flex items-center gap-2 flex-wrap">
                    <div className="relative">
                      <button
                        type="button"
                        disabled={batchBusy || selectedMessageIds.size === 0}
                        onClick={() => setBatchMoveOpen((prev) => !prev)}
                        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-medium bg-[#FAF8F5] hover:bg-[#F1ECE5] border border-[#E4DDD2] text-[#262422] disabled:opacity-40 transition-colors"
                      >
                        <FolderInput className="w-3.5 h-3.5 text-[#5B7A95]" />
                        <span>移至话题</span>
                      </button>

                      {batchMoveOpen && (
                        <div className="absolute bottom-full right-0 mb-2 w-56 rounded-xl bg-white border border-[#E8E2D9] shadow-xl shadow-stone-900/10 p-1.5 z-50">
                          <div className="px-2.5 py-1 text-[10px] font-medium uppercase tracking-wider text-[#8C8378]">
                            选择目标话题
                          </div>
                          <div className="max-h-48 overflow-y-auto space-y-0.5">
                            {topics
                              .filter((t) => t.status === 'active')
                              .map((t) => (
                                <button
                                  key={t.id}
                                  type="button"
                                  disabled={batchBusy}
                                  onClick={() => {
                                    setBatchMoveOpen(false);
                                    void handleBatchReassign({
                                      action: 'move',
                                      target_topic_id: t.id,
                                    });
                                  }}
                                  className="w-full text-left px-2.5 py-1.5 rounded-lg text-xs text-[#3A3530] hover:bg-[#FAF8F5] truncate transition-colors"
                                >
                                  #{t.title}
                                </button>
                              ))}
                          </div>
                        </div>
                      )}
                    </div>

                    <button
                      type="button"
                      disabled={batchBusy || selectedMessageIds.size === 0}
                      onClick={() => {
                        setBatchMoveOpen(false);
                        setBatchSplitOpen(true);
                      }}
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-medium bg-[#5B7A95] hover:bg-[#4B6882] text-white disabled:opacity-40 transition-colors"
                    >
                      <GitBranchPlus className="w-3.5 h-3.5" />
                      <span>拆为新话题</span>
                    </button>

                    <button
                      type="button"
                      onClick={exitSelectionMode}
                      className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-xl text-xs text-[#7A7269] hover:text-[#262422] hover:bg-[#F3EFE9] transition-colors"
                    >
                      <X className="w-3.5 h-3.5" />
                      <span>取消</span>
                    </button>
                  </div>
                )}
              </div>
            ) : (
              <>
            {/* Subtle Path Toggle Row */}
            <div className="flex items-center justify-between gap-2 mb-1.5 px-1 flex-wrap">
              <div className="flex items-center gap-2 flex-wrap">
                <div className="inline-flex items-center gap-1 p-0.5 rounded-xl bg-[#F1ECE5] border border-[#E5DED4]">
                  {(
                    [
                      { id: 'auto', label: '自动路由', icon: Compass },
                      { id: 'fast', label: '轻量', icon: Zap },
                      { id: 'heavy', label: '重型', icon: Cpu },
                    ] as const
                  ).map((opt) => {
                    const Icon = opt.icon;
                    const active = pathOption === opt.id;
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        onClick={() => setPathOption(opt.id)}
                        className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-[11px] font-medium transition-all ${active
                            ? 'bg-white text-[#262422] shadow-sm'
                            : 'text-[#7A7269] hover:text-[#262422]'
                          }`}
                      >
                        <Icon
                          className={`w-3 h-3 ${active ? 'text-[#5B7A95]' : 'text-[#9A9187]'
                            }`}
                        />
                        {opt.label}
                      </button>
                    );
                  })}
                </div>

                {adapters.length > 0 && pathOption !== 'fast' && (
                  <select
                    aria-label="选择重型任务执行器"
                    value={selectedAgentType}
                    onChange={(e) => setSelectedAgentType(e.target.value)}
                    title={
                      selectedAgentType === 'auto'
                        ? '由 Kiki 自动识别并选择最合适的执行引擎'
                        : adapters.find((a) => a.id === selectedAgentType)?.install_hint ??
                          '选择重型任务 Adapter'
                    }
                    className="h-7 px-2 rounded-xl text-[11px] font-medium bg-white text-[#4E4740] border border-[#E5DED4] focus:outline-none focus:border-[#5B7A95] cursor-pointer"
                  >
                    <option value="auto">✦ 自动选择引擎</option>
                    {adapters.map((adapter) => (
                      <option key={adapter.id} value={adapter.id}>
                        {adapter.available ? '● ' : '○ '}
                        {adapter.name}
                      </option>
                    ))}
                  </select>
                )}
              </div>

              <div className="flex items-center gap-2">
                {activeRunCount > 0 && (
                  <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium bg-[#EAF0F5] text-[#44637D] border border-[#CFE0EC]">
                    <span className="w-1.5 h-1.5 rounded-full bg-[#5B7A95] animate-pulse" />
                    <span>{activeRunCount} 个任务并行中 · 可随时继续输入</span>
                  </span>
                )}
                <span className="text-[11px] text-[#9A9187] hidden sm:inline">
                  Enter 发送 · Shift + Enter 换行
                </span>
              </div>
            </div>

            {/* Input Container */}
            <div className="rounded-2xl bg-white border border-[#E4DDD2] focus-within:border-[#5B7A95] focus-within:ring-2 focus-within:ring-[#5B7A95]/15 shadow-sm transition-all flex items-end gap-2 px-3.5 py-2.5">
              <textarea
                id="kiki-chat-input"
                name="message"
                ref={textareaRef}
                rows={1}
                value={inputText}
                onChange={handleTextareaChange}
                onKeyDown={handleKeyDown}
                placeholder={
                  focusedTopic
                    ? `在 #${focusedTopic.title} 中继续对话...`
                    : activeRunCount > 0
                      ? '后台任务并行执行中，随时继续发新消息或开启新话题...'
                      : '和 Kiki 聊点什么，或直接交办一个后台任务...'
                }
                className="flex-1 resize-none bg-transparent text-sm text-[#262422] placeholder:text-[#A39A90] focus:outline-none max-h-40 leading-relaxed py-0.5"
              />

              <button
                type="button"
                disabled={!inputText.trim()}
                onClick={() => handleSend()}
                aria-label="发送消息"
                className="w-9 h-9 rounded-xl bg-[#5B7A95] hover:bg-[#4B6882] text-white flex items-center justify-center shrink-0 transition-all active:scale-95 disabled:opacity-40 disabled:hover:bg-[#5B7A95]"
              >
                <Send className="w-4 h-4" />
              </button>
            </div>
              </>
            )}
          </div>
        </div>
      </main>

      {/* L3 Geek Log Drawer */}
      <LogDrawer
        run={activeDrawerRun}
        rawLogs={activeDrawerRunId ? rawLogsByRunId[activeDrawerRunId] ?? [] : []}
        onClose={() => setActiveDrawerRunId(null)}
      />

      {/* Cross-Topic Async Toast Stack */}
      <ToastContainer
        toasts={toasts}
        onSelectTopic={handleSelectTopic}
        onDismiss={dismissToast}
      />
    </div>
  );
};

export default App;
