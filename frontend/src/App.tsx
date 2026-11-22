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
  fetchAdapters,
  fetchHealth,
  fetchMessages,
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
import UnifiedMessageList from './components/UnifiedMessageList';
import LogDrawer from './components/LogDrawer';
import ToastContainer from './components/ToastContainer';

type PathOption = 'auto' | 'fast' | 'heavy';

function upsertTopic(list: Topic[], incoming: Topic): Topic[] {
  const idx = list.findIndex((t) => t.id === incoming.id);
  const next =
    idx >= 0
      ? list.map((t, i) => (i === idx ? { ...t, ...incoming } : t))
      : [incoming, ...list];
  return next.sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''));
}

function upsertMessage(list: Message[], incoming: Message): Message[] {
  const idx = list.findIndex((m) => m.id === incoming.id);
  if (idx >= 0) {
    return list.map((m, i) =>
      i === idx
        ? {
          ...m,
          ...incoming,
          session_run: incoming.session_run ?? m.session_run,
          topic: incoming.topic ?? m.topic,
        }
        : m
    );
  }
  return [...list, incoming];
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

function mergeRun(prev: SessionRun | undefined, incoming: SessionRun): SessionRun {
  if (!prev) return incoming;
  return {
    ...prev,
    ...incoming,
    step_logs:
      incoming.step_logs && incoming.step_logs.length > 0
        ? incoming.step_logs
        : prev.step_logs,
    artifacts:
      incoming.artifacts && incoming.artifacts.length > 0
        ? incoming.artifacts
        : prev.artifacts,
    raw_logs:
      incoming.raw_logs && incoming.raw_logs.length > 0
        ? incoming.raw_logs
        : prev.raw_logs,
    hitl_request:
      incoming.status === 'waiting_input'
        ? incoming.hitl_request ?? prev.hitl_request
        : incoming.hitl_request ?? null,
  };
}

export const App: React.FC = () => {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [adapters, setAdapters] = useState<AdapterInfo[]>([]);
  const [selectedAgentType, setSelectedAgentType] = useState<string>('gemini_agent');
  const [topics, setTopics] = useState<Topic[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [runsById, setRunsById] = useState<Record<string, SessionRun>>({});
  const [rawLogsByRunId, setRawLogsByRunId] = useState<Record<string, RawLogLine[]>>({});

  const [focusTopicId, setFocusTopicId] = useState<string | null>(null);
  const focusTopicIdRef = useRef<string | null>(null);
  focusTopicIdRef.current = focusTopicId;

  const [serverKikiStatus, setServerKikiStatus] = useState<KikiStatus>('idle');
  const [sseConnected, setSseConnected] = useState(false);
  const [sending, setSending] = useState(false);
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
  const pendingOptimisticRef = useRef<Array<{ tempId: string; content: string }>>([]);
  const resolvedOptimisticMapRef = useRef<Map<string, string>>(new Map());

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

  // Hydrate initial state
  const loadInitialData = useCallback(async () => {
    try {
      const [healthRes, adaptersRes, topicsRes, messagesRes] = await Promise.all([
        fetchHealth().catch(() => null),
        fetchAdapters().catch(() => []),
        fetchTopics(),
        fetchMessages(),
      ]);
      if (healthRes) setHealth(healthRes);
      if (adaptersRes.length > 0) setAdapters(adaptersRes);
      setTopics(topicsRes);
      setMessages(messagesRes);

      const initialRuns: Record<string, SessionRun> = {};
      for (const msg of messagesRes) {
        if (msg.session_run) {
          initialRuns[msg.session_run.id] = msg.session_run;
        }
      }
      setRunsById((prev) => ({ ...prev, ...initialRuns }));
    } catch (err) {
      setErrorBanner(
        err instanceof Error ? err.message : '加载初始数据失败，请检查后端服务是否已启动'
      );
    }
  }, []);

  useEffect(() => {
    loadInitialData();
  }, [loadInitialData]);

  // Subscribe to global SSE event stream
  useEffect(() => {
    const disconnect = connectEventStream({
      onOpen: () => {
        setSseConnected(true);
        setErrorBanner(null);
      },
      onError: () => {
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
          const matchIdx = pendingOptimisticRef.current.findIndex(
            (item) => item.content === message.content
          );
          if (matchIdx >= 0) {
            const [matched] = pendingOptimisticRef.current.splice(matchIdx, 1);
            resolvedOptimisticMapRef.current.set(matched.tempId, message.id);
            setMessages((prev) =>
              replaceOptimisticOrUpsert(prev, matched.tempId, message)
            );
          } else {
            setMessages((prev) => upsertMessage(prev, message));
          }
        } else {
          setMessages((prev) => upsertMessage(prev, message));
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
        setMessages((prev) => {
          const exists = prev.some((m) => m.id === message_id);
          if (!exists) {
            return [
              ...prev,
              {
                id: message_id,
                topic_id,
                role: 'assistant',
                content: delta,
                created_at: new Date().toISOString(),
                streaming: true,
              },
            ];
          }
          return prev.map((m) =>
            m.id === message_id
              ? { ...m, content: (m.content || '') + delta, streaming: true }
              : m
          );
        });
      },
      onMessageCompleted: ({ message }) => {
        setMessages((prev) =>
          upsertMessage(prev, { ...message, streaming: false })
        );
      },
      onMessageUpdated: ({ message }) => {
        setMessages((prev) => upsertMessage(prev, message));
      },
      onRunCreated: ({ run }) => {
        setRunsById((prev) => ({
          ...prev,
          [run.id]: mergeRun(prev[run.id], run),
        }));
        // Ensure trigger message points to this run
        if (run.trigger_message_id) {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === run.trigger_message_id && !m.session_run
                ? { ...m, session_run: run }
                : m
            )
          );
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
  }, [pushToast]);

  const topicsById = useMemo(() => {
    const map: Record<string, Topic> = {};
    for (const t of topics) {
      map[t.id] = t;
    }
    return map;
  }, [topics]);

  const visibleMessages = useMemo(() => {
    if (!focusTopicId) return messages;
    return messages.filter((m) => m.topic_id === focusTopicId);
  }, [messages, focusTopicId]);

  const focusedTopic = focusTopicId ? topicsById[focusTopicId] ?? null : null;

  const hasStreamingMessage = useMemo(
    () => messages.some((m) => m.streaming),
    [messages]
  );

  const isDelivering = useMemo(() => {
    const allRuns = Object.values(runsById);
    return (
      sending ||
      toasts.length > 0 ||
      allRuns.some((r) => r.status === 'queued')
    );
  }, [runsById, sending, toasts.length]);

  const globalKikiStatus: KikiStatus = useMemo(() => {
    const allRuns = Object.values(runsById);
    if (allRuns.some((r) => r.status === 'waiting_input')) {
      return 'waiting_input';
    }
    if (sending || hasStreamingMessage || serverKikiStatus === 'thinking') {
      return 'thinking';
    }
    if (allRuns.some((r) => r.status === 'running' || r.status === 'queued')) {
      return 'running';
    }
    return serverKikiStatus;
  }, [runsById, sending, hasStreamingMessage, serverKikiStatus]);

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
    },
    [topics]
  );

  const handleSelectAllStream = useCallback(() => {
    setFocusTopicId(null);
    setMobileSidebarOpen(false);
  }, []);

  const handleCreateTopic = useCallback(async (title: string) => {
    try {
      const created = await createTopic({ title });
      setTopics((prev) => upsertTopic(prev, created));
      setFocusTopicId(created.id);
    } catch (err) {
      setErrorBanner(err instanceof Error ? err.message : '创建话题失败');
    }
  }, []);

  const handleSend = useCallback(
    async (overrideText?: string) => {
      const content = (overrideText ?? inputText).trim();
      if (!content || sending) return;

      setSending(true);
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
        topic_id: focusTopicId ?? '__routing__',
        role: 'user',
        content,
        created_at: new Date().toISOString(),
        metadata: {
          optimistic: true,
          routing: !focusTopicId,
        },
        topic: focusTopicId ? topicsById[focusTopicId] ?? null : null,
      };
      pendingOptimisticRef.current.push({ tempId, content });
      setMessages((prev) => [...prev, optimisticUser]);

      try {
        const res = await sendChat({
          content,
          focus_topic_id: focusTopicId,
          force_path: pathOption === 'auto' ? null : pathOption,
          agent_type: selectedAgentType,
        });

        if (res.topic) {
          setTopics((prev) => upsertTopic(prev, res.topic));
        }
        if (res.reassigned_messages && res.reassigned_messages.length > 0) {
          setMessages((prev) =>
            res.reassigned_messages!.reduce(
              (acc, msg) =>
                upsertMessage(acc, {
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
          setMessages((prev) =>
            replaceOptimisticOrUpsert(prev, tempId, enrichedUser)
          );
        }
        if (res.assistant_message) {
          const enrichedAssistant: Message = {
            ...res.assistant_message,
            topic: res.topic ?? res.assistant_message.topic,
            session_run: res.session_run ?? res.assistant_message.session_run,
          };
          setMessages((prev) => upsertMessage(prev, enrichedAssistant));
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
        setMessages((prev) => prev.filter((m) => m.id !== tempId));
        setErrorBanner(
          err instanceof Error ? err.message : '发送消息失败，请稍后重试'
        );
        if (!overrideText) {
          setInputText(content);
        }
      } finally {
        setSending(false);
      }
    },
    [inputText, sending, focusTopicId, pathOption, selectedAgentType, topicsById]
  );

  const handleReassignMessage = useCallback(
    async (messageId: string, payload: ReassignTopicPayload) => {
      try {
        const res = await reassignMessageTopic(messageId, payload);
        if (res.topic) {
          setTopics((prev) => upsertTopic(prev, res.topic));
        }
        if (res.message) {
          setMessages((prev) =>
            upsertMessage(prev, {
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
          setMessages((prev) =>
            res.messages.reduce(
              (acc, msg) =>
                upsertMessage(acc, {
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
          isDelivering={isDelivering}
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
              isDelivering={isDelivering}
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
          sending={sending}
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
                      adapters.find((a) => a.id === selectedAgentType)?.install_hint ??
                      '选择重型任务 Adapter'
                    }
                    className="h-7 px-2 rounded-xl text-[11px] font-medium bg-white text-[#4E4740] border border-[#E5DED4] focus:outline-none focus:border-[#5B7A95] cursor-pointer"
                  >
                    {adapters.map((adapter) => (
                      <option key={adapter.id} value={adapter.id}>
                        {adapter.available ? '● ' : '○ '}
                        {adapter.name}
                      </option>
                    ))}
                  </select>
                )}
              </div>

              <span className="text-[11px] text-[#9A9187] hidden sm:inline">
                Enter 发送 · Shift + Enter 换行
              </span>
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
                    : '和 Kiki 聊点什么，或直接交办一个后台任务...'
                }
                className="flex-1 resize-none bg-transparent text-sm text-[#262422] placeholder:text-[#A39A90] focus:outline-none max-h-40 leading-relaxed py-0.5"
              />

              <button
                type="button"
                disabled={!inputText.trim() || sending}
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
