import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDown,
  Check,
  ChevronUp,
  CornerUpLeft,
  GripVertical,
  Loader2,
  MessageCircle,
  ShieldAlert,
  Sparkles,
  Terminal,
  ZoomIn,
} from 'lucide-react';
import type {
  Message,
  QuotedMessageSnapshot,
  ReassignTopicPayload,
  ResumeRunPayload,
  SessionRun,
  Topic,
} from '../types';
import KikiAvatar from './KikiAvatar';
import UserAvatar, { useUserProfile } from './UserAvatar';
import TopicBadge from './TopicBadge';
import RunStatusCard from './RunStatusCard';
import {
  getArtifactUrl,
  isImageArtifact,
  type ArtifactPreviewItem,
} from './ArtifactPreviewModal';

export interface UnifiedMessageListProps {
  messages: Message[];
  topicsById: Record<string, Topic>;
  allTopics: Topic[];
  runsById: Record<string, SessionRun>;
  focusTopicId: string | null;
  showAllTopicTags?: boolean;
  hasMore?: boolean;
  loadingInitial?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void;
  sending?: boolean;
  pendingSendCount?: number;
  selectionMode?: boolean;
  selectedMessageIds?: Set<string>;
  onToggleSelectMessage?: (messageId: string, shiftKey?: boolean) => void;
  onStartMultiSelect?: (messageId: string) => void;
  onQuoteMessage?: (message: Message) => void;
  onSelectTopic: (topicId: string) => void;
  onReassignMessage: (
    messageId: string,
    payload: ReassignTopicPayload
  ) => Promise<void> | void;
  onOpenLogDrawer: (runId: string) => void;
  onResumeRun: (runId: string, payload: ResumeRunPayload) => Promise<void> | void;
  onCancelRun: (runId: string) => Promise<void> | void;
  onRetryRun?: (runId: string) => Promise<void> | void;
  onPreviewArtifact?: (items: ArtifactPreviewItem[], initialIndex: number) => void;
  onOpenDesktopTakeover?: (ctx: import('../types').DesktopTakeoverContext) => void;
  onStarterPrompt: (prompt: string) => void;
}

const STARTER_PROMPTS = [
  {
    icon: MessageCircle,
    tag: 'Fast-Path 轻量陪聊',
    title: '和 Kiki 打个招呼',
    prompt: '嗨 Kiki，今天感觉有点累，给我讲个简短的猫咪冷笑话提提神吧！',
    accent: 'text-[#5B7A95] bg-[#EAF0F5]',
  },
  {
    icon: Terminal,
    tag: 'Heavy-Path 后台巡检',
    title: '巡检当前项目工作区',
    prompt: '帮我检查当前仓库的目录结构和 README 或设计文档，总结一下项目目前的核心模块。',
    accent: 'text-[#3B6244] bg-[#ECF3EC]',
  },
  {
    icon: ShieldAlert,
    tag: 'Heavy-Path + HITL 拍板',
    title: '体验人工确认审批流',
    prompt: '帮我在工作区执行一次临时日志清理检查：先列出需要处理的项目，在执行任何删除或修改前务必请求我拍板确认。',
    accent: 'text-[#B45309] bg-amber-50',
  },
];

function formatTime(ts?: string | null): string {
  if (!ts) return '';
  const normalized = ts.includes('T')
    ? ts
    : `${ts.replace(' ', 'T')}${ts.endsWith('Z') ? '' : 'Z'}`;
  const d = new Date(normalized);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function renderLinksAndText(text: string, keyPrefix: string): React.ReactNode {
  const linkRe = /(\[[^\]]+\]\(https?:\/\/[^\s)]+\)|https?:\/\/[^\s<>()]+)/g;
  const parts = text.split(linkRe);
  return parts.map((part, idx) => {
    if (!part) return null;
    const mdMatch = part.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
    if (mdMatch) {
      return (
        <a
          key={`${keyPrefix}-l-${idx}`}
          href={mdMatch[2]}
          target="_blank"
          rel="noopener noreferrer"
          className="text-[#456B8A] underline decoration-[#456B8A]/40 hover:decoration-[#456B8A] break-all"
        >
          {mdMatch[1]}
        </a>
      );
    }
    if (/^https?:\/\/\S+$/.test(part)) {
      return (
        <a
          key={`${keyPrefix}-u-${idx}`}
          href={part}
          target="_blank"
          rel="noopener noreferrer"
          className="text-[#456B8A] underline decoration-[#456B8A]/40 hover:decoration-[#456B8A] break-all"
        >
          {part}
        </a>
      );
    }
    return <React.Fragment key={`${keyPrefix}-t-${idx}`}>{part}</React.Fragment>;
  });
}

function renderInlineCode(text: string, keyPrefix: string): React.ReactNode {
  const codeParts = text.split(/(`[^`]+`)/g);
  return codeParts.map((part, idx) => {
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code
          key={`${keyPrefix}-c-${idx}`}
          className="px-1.5 py-0.5 mx-0.5 rounded bg-[#F3EFE9] text-[#3B5972] font-mono text-[12px]"
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    return (
      <React.Fragment key={`${keyPrefix}-r-${idx}`}>
        {renderLinksAndText(part, `${keyPrefix}-r-${idx}`)}
      </React.Fragment>
    );
  });
}

function renderInlineMarkdown(text: string, keyPrefix = 'm'): React.ReactNode {
  const parts = text.split(/(\*\*[^*\n]+\*\*)/g);
  return parts.map((part, idx) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return (
        <strong key={`${keyPrefix}-b-${idx}`} className="font-semibold text-[#1E1C1A]">
          {renderInlineCode(part.slice(2, -2), `${keyPrefix}-b-${idx}`)}
        </strong>
      );
    }
    return (
      <React.Fragment key={`${keyPrefix}-p-${idx}`}>
        {renderInlineCode(part, `${keyPrefix}-p-${idx}`)}
      </React.Fragment>
    );
  });
}

function renderMarkdownContent(text: string): React.ReactNode {
  if (!text) return null;
  const segments = text.split(/(```[\s\S]*?```)/g);
  return segments.map((seg, segIdx) => {
    if (seg.startsWith('```') && seg.endsWith('```') && seg.length >= 6) {
      const inner = seg.slice(3, -3);
      const nlIdx = inner.indexOf('\n');
      const lang = nlIdx >= 0 ? inner.slice(0, nlIdx).trim() : '';
      const code = nlIdx >= 0 ? inner.slice(nlIdx + 1) : inner;
      return (
        <div
          key={`code-${segIdx}`}
          className="my-2 rounded-xl bg-[#1F1E1C] text-[#EAE6DF] border border-[#34312D] overflow-hidden"
        >
          {lang && (
            <div className="px-3 py-1 text-[10px] font-mono uppercase tracking-wider text-[#9A938A] bg-[#282623] border-b border-[#34312D]">
              {lang}
            </div>
          )}
          <pre className="p-3 overflow-x-auto text-[12px] font-mono leading-relaxed whitespace-pre">
            <code>{code.replace(/\n$/, '')}</code>
          </pre>
        </div>
      );
    }

    const lines = seg.split('\n');
    return (
      <React.Fragment key={`txt-${segIdx}`}>
        {lines.map((line, lineIdx) => {
          const headingMatch = line.match(/^(#{1,3})\s+(.+)$/);
          if (headingMatch) {
            const level = headingMatch[1].length;
            const cls =
              level === 1
                ? 'text-base font-bold text-[#1E1C1A] mt-2 mb-1'
                : level === 2
                  ? 'text-[15px] font-semibold text-[#1E1C1A] mt-2 mb-1'
                  : 'text-sm font-semibold text-[#262422] mt-1.5 mb-0.5';
            return (
              <div key={`h-${segIdx}-${lineIdx}`} className={cls}>
                {renderInlineMarkdown(headingMatch[2], `h-${segIdx}-${lineIdx}`)}
              </div>
            );
          }
          const bulletMatch = line.match(/^\s*[-*•]\s+(.+)$/);
          if (bulletMatch) {
            return (
              <div key={`li-${segIdx}-${lineIdx}`} className="flex items-start gap-2 pl-1 my-0.5">
                <span className="text-[#5B7A95] select-none leading-relaxed">•</span>
                <span className="flex-1 min-w-0">
                  {renderInlineMarkdown(bulletMatch[1], `li-${segIdx}-${lineIdx}`)}
                </span>
              </div>
            );
          }
          const orderedMatch = line.match(/^\s*(\d+\.)\s+(.+)$/);
          if (orderedMatch) {
            return (
              <div key={`ol-${segIdx}-${lineIdx}`} className="flex items-start gap-2 pl-1 my-0.5">
                <span className="text-[#5B7A95] font-mono text-xs select-none leading-relaxed">
                  {orderedMatch[1]}
                </span>
                <span className="flex-1 min-w-0">
                  {renderInlineMarkdown(orderedMatch[2], `ol-${segIdx}-${lineIdx}`)}
                </span>
              </div>
            );
          }
          return (
            <React.Fragment key={`ln-${segIdx}-${lineIdx}`}>
              {renderInlineMarkdown(line, `ln-${segIdx}-${lineIdx}`)}
              {lineIdx < lines.length - 1 ? '\n' : null}
            </React.Fragment>
          );
        })}
      </React.Fragment>
    );
  });
}

export const UnifiedMessageList: React.FC<UnifiedMessageListProps> = ({
  messages,
  topicsById,
  allTopics,
  runsById,
  focusTopicId,
  showAllTopicTags = false,
  hasMore = false,
  loadingInitial = false,
  loadingOlder = false,
  onLoadOlder,
  sending = false,
  pendingSendCount = 0,
  selectionMode = false,
  selectedMessageIds,
  onToggleSelectMessage,
  onStartMultiSelect,
  onQuoteMessage,
  onSelectTopic,
  onReassignMessage,
  onOpenLogDrawer,
  onResumeRun,
  onCancelRun,
  onRetryRun,
  onPreviewArtifact,
  onOpenDesktopTakeover,
  onStarterPrompt,
}) => {
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const messageRowRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const highlightTimerRef = useRef<number | null>(null);
  const longPressTimerRef = useRef<number | null>(null);
  const touchStartPosRef = useRef<{ x: number; y: number } | null>(null);
  const longPressTriggeredRef = useRef<boolean>(false);
  const [userProfile] = useUserProfile();
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const [highlightedMessageId, setHighlightedMessageId] = useState<string | null>(null);

  const messagesById = useMemo(() => {
    const map: Record<string, Message> = {};
    for (const m of messages) {
      map[m.id] = m;
    }
    return map;
  }, [messages]);

  const clearLongPressTimer = useCallback(() => {
    if (longPressTimerRef.current !== null) {
      window.clearTimeout(longPressTimerRef.current);
      longPressTimerRef.current = null;
    }
    touchStartPosRef.current = null;
  }, []);

  useEffect(() => {
    return () => {
      clearLongPressTimer();
      if (highlightTimerRef.current !== null) {
        window.clearTimeout(highlightTimerRef.current);
      }
    };
  }, [clearLongPressTimer]);

  const handleJumpToMessage = useCallback((targetId: string) => {
    const targetEl = messageRowRefs.current[targetId];
    if (!targetEl) return;
    targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setHighlightedMessageId(targetId);
    if (highlightTimerRef.current !== null) {
      window.clearTimeout(highlightTimerRef.current);
    }
    highlightTimerRef.current = window.setTimeout(() => {
      setHighlightedMessageId((prev) => (prev === targetId ? null : prev));
      highlightTimerRef.current = null;
    }, 1600);
  }, []);

  const getQuotedSnapshot = useCallback(
    (msg: Message): QuotedMessageSnapshot | null => {
      const snap = msg.metadata?.quoted_message;
      if (snap && typeof snap === 'object' && typeof snap.id === 'string') {
        return snap;
      }
      const qId = msg.metadata?.quoted_message_id;
      if (typeof qId === 'string' && messagesById[qId]) {
        const refMsg = messagesById[qId];
        return {
          id: refMsg.id,
          role: refMsg.role,
          content: refMsg.content,
          topic_id: refMsg.topic_id,
          topic_title: topicsById[refMsg.topic_id]?.title ?? refMsg.topic?.title ?? null,
          created_at: refMsg.created_at,
        };
      }
      return null;
    },
    [messagesById, topicsById]
  );

  const viewKey = focusTopicId ?? '__all__';
  const prevViewKeyRef = useRef<string>(viewKey);
  const prevSnapshotRef = useRef<{
    firstId: string | null;
    lastId: string | null;
    lastContentLen: number;
    count: number;
  }>({
    firstId: null,
    lastId: null,
    lastContentLen: 0,
    count: 0,
  });
  const prevScrollHeightRef = useRef<number>(0);
  const prevScrollTopRef = useRef<number>(0);
  const isNearBottomRef = useRef<boolean>(true);

  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    prevScrollTopRef.current = el.scrollTop;
    prevScrollHeightRef.current = el.scrollHeight;

    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    isNearBottomRef.current = distanceFromBottom < 140;
    setShowScrollToBottom(distanceFromBottom > 420);

    if (
      el.scrollTop <= 140 &&
      hasMore &&
      !loadingOlder &&
      !loadingInitial &&
      onLoadOlder
    ) {
      onLoadOlder();
    }
  }, [hasMore, loadingOlder, loadingInitial, onLoadOlder]);

  const handleJumpToBottom = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    isNearBottomRef.current = true;
    setShowScrollToBottom(false);
  }, []);

  useLayoutEffect(() => {
    const el = scrollContainerRef.current;
    const firstMsg = messages[0] ?? null;
    const lastMsg = messages[messages.length - 1] ?? null;
    const firstId = firstMsg?.id ?? null;
    const lastId = lastMsg?.id ?? null;
    const lastContentLen = lastMsg?.content?.length ?? 0;
    const count = messages.length;

    const prev = prevSnapshotRef.current;
    const viewChanged = prevViewKeyRef.current !== viewKey;
    prevViewKeyRef.current = viewKey;

    if (!el) {
      prevSnapshotRef.current = { firstId, lastId, lastContentLen, count };
      if (viewChanged) {
        isNearBottomRef.current = true;
        setShowScrollToBottom(false);
      }
      return;
    }

    if (viewChanged) {
      isNearBottomRef.current = true;
      setShowScrollToBottom(false);
      if (count > 0 && !loadingInitial) {
        el.scrollTop = el.scrollHeight;
      }
    } else if (prev.count === 0 && count > 0) {
      // First hydrated render for this view
      el.scrollTop = el.scrollHeight;
      isNearBottomRef.current = true;
      setShowScrollToBottom(false);
    } else if (
      prev.firstId !== null &&
      count > prev.count &&
      firstId !== prev.firstId &&
      messages.some((m) => m.id === prev.firstId)
    ) {
      // Older messages were prepended at the top: preserve exact viewport anchor
      const heightDelta = el.scrollHeight - prevScrollHeightRef.current;
      if (heightDelta > 0) {
        el.scrollTop = prevScrollTopRef.current + heightDelta;
      }
    } else if (
      (lastId !== null && lastId !== prev.lastId) ||
      (sending && isNearBottomRef.current)
    ) {
      const isUserOrOptimistic = Boolean(
        lastMsg && (lastMsg.role === 'user' || lastMsg.metadata?.optimistic)
      );
      if (isUserOrOptimistic || isNearBottomRef.current) {
        el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
        isNearBottomRef.current = true;
        setShowScrollToBottom(false);
      }
    } else if (
      lastId !== null &&
      lastId === prev.lastId &&
      lastContentLen > prev.lastContentLen &&
      isNearBottomRef.current
    ) {
      // Streaming delta grew the bottom message while user is following along
      el.scrollTop = el.scrollHeight;
    }

    prevSnapshotRef.current = { firstId, lastId, lastContentLen, count };
    prevScrollHeightRef.current = el.scrollHeight;
    prevScrollTopRef.current = el.scrollTop;
  }, [messages, viewKey, loadingInitial, loadingOlder, sending]);

  if (loadingInitial && messages.length === 0) {
    return (
      <div className="flex-1 overflow-y-auto px-4 sm:px-8 py-10 flex flex-col items-center justify-center">
        <div className="inline-flex items-center gap-2.5 px-4 py-2.5 rounded-2xl bg-white border border-[#E8E2D9] shadow-sm text-xs text-[#6F6860]">
          <Loader2 className="w-4 h-4 text-[#5B7A95] animate-spin" />
          <span>{focusTopicId ? '正在加载话题记录...' : '正在加载最近聊天记录...'}</span>
        </div>
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div className="flex-1 overflow-y-auto px-4 sm:px-8 py-10 flex flex-col items-center justify-center">
        <div className="max-w-xl w-full text-center space-y-6 animate-kiki-fade-up">
          <div className="flex flex-col items-center gap-2">
            {/* Static by default so she doesn't sync-blink with bottom-left Kiki, but responds if clicked! */}
            <KikiAvatar status="idle" animated={false} companion size="hero" />
            <div>
              <h2 className="text-xl font-semibold text-[#262422] tracking-tight">
                {focusTopicId ? '这个话题还没有消息' : '你好，我是 Kiki'}
              </h2>
              <p className="mt-1.5 text-sm text-[#787067] leading-relaxed max-w-md mx-auto">
                无需先点新建对话，随时发消息。轻聊前台秒回，重活后台跑腿并自动归类到对应{' '}
                <span className="font-medium text-[#5B7A95]">#Topic</span>。
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-left">
            {STARTER_PROMPTS.map((item) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.title}
                  type="button"
                  onClick={() => onStarterPrompt(item.prompt)}
                  className="group rounded-2xl bg-white border border-[#E8E2D9] hover:border-[#5B7A95]/60 hover:shadow-md hover:shadow-stone-900/5 p-3.5 flex flex-col justify-between gap-2.5 transition-all text-left"
                >
                  <div className="space-y-2">
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold ${item.accent}`}
                    >
                      <Icon className="w-3 h-3" />
                      {item.tag}
                    </span>
                    <div className="text-sm font-semibold text-[#262422] group-hover:text-[#44637D] transition-colors">
                      {item.title}
                    </div>
                    <p className="text-xs text-[#787067] leading-relaxed">
                      {item.prompt}
                    </p>
                  </div>
                  <span className="inline-flex items-center gap-1 text-[11px] font-medium text-[#5B7A95] opacity-80 group-hover:opacity-100">
                    <Sparkles className="w-3 h-3" />
                    点击试一试
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    );
  }

  // Pre-compute the latest streaming message ID and latest active run per status
  // so no two cats in the message list ever animate the same loop simultaneously.
  let latestStreamingMsgId: string | null = null;
  const preSeenRunIds = new Set<string>();
  const latestRunIdByStatus: Partial<Record<SessionRun['status'], string>> = {};

  for (const msg of messages) {
    if (Boolean(msg.streaming ?? msg.metadata?.streaming)) {
      latestStreamingMsgId = msg.id;
    }
    const rId =
      msg.session_run?.id ??
      (typeof msg.metadata?.session_run_id === 'string'
        ? msg.metadata.session_run_id
        : undefined);
    const cand = rId ? runsById[rId] ?? msg.session_run ?? null : msg.session_run ?? null;
    if (cand && !preSeenRunIds.has(cand.id)) {
      preSeenRunIds.add(cand.id);
      if (
        cand.status === 'queued' ||
        cand.status === 'running' ||
        cand.status === 'waiting_input'
      ) {
        latestRunIdByStatus[cand.status] = cand.id;
      }
    }
  }

  const renderedRunIds = new Set<string>();

  return (
    <div className="relative flex-1 min-h-0 flex flex-col">
      <div
        ref={scrollContainerRef}
        onScroll={handleScroll}
        style={{ overflowAnchor: 'none' }}
        className="flex-1 overflow-y-auto px-4 sm:px-6 lg:px-8 py-6"
      >
        <div className="max-w-3xl mx-auto space-y-5">
          {/* Top History Pagination Header (fixed height to prevent layout shift) */}
          <div className="h-9 flex items-center justify-center">
            {loadingOlder ? (
              <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#F3EFE9] border border-[#E6DFD5] text-[11px] text-[#6F6860]">
                <Loader2 className="w-3 h-3 text-[#5B7A95] animate-spin" />
                <span>正在加载更早的消息...</span>
              </span>
            ) : hasMore ? (
              <button
                type="button"
                onClick={() => onLoadOlder?.()}
                className="inline-flex items-center gap-1 px-3 py-1 rounded-full bg-white hover:bg-[#F3EFE9] border border-[#E6DFD5] text-[11px] font-medium text-[#5B7A95] transition-colors shadow-sm shadow-stone-900/[0.02]"
              >
                <ChevronUp className="w-3.5 h-3.5" />
                <span>加载更早的消息</span>
              </button>
            ) : (
              <span className="text-[11px] text-[#ABA298] select-none">
                — 已加载全部历史消息 —
              </span>
            )}
          </div>

          {messages.map((msg) => {
          const isUser = msg.role === 'user';
          const isSystem = msg.role === 'system';
          const isSelectable = !isSystem && !msg.metadata?.optimistic;
          const isSelected = Boolean(selectedMessageIds?.has(msg.id));
          const topic = topicsById[msg.topic_id] ?? msg.topic ?? null;
          const confidence =
            typeof msg.metadata?.confidence === 'number'
              ? msg.metadata.confidence
              : undefined;
          const runId =
            msg.session_run?.id ??
            (typeof msg.metadata?.session_run_id === 'string'
              ? msg.metadata.session_run_id
              : undefined);
          const candidateRun = runId
            ? runsById[runId] ?? msg.session_run ?? null
            : msg.session_run ?? null;
          let sessionRun: SessionRun | null = null;
          if (candidateRun && !renderedRunIds.has(candidateRun.id)) {
            renderedRunIds.add(candidateRun.id);
            sessionRun = candidateRun;
          }

          const shouldAnimateRun = Boolean(
            sessionRun &&
              latestRunIdByStatus[sessionRun.status] === sessionRun.id &&
              !(sending && sessionRun.status === 'queued')
          );

          const handleDragStart = (e: React.DragEvent<HTMLElement>) => {
            if (selectionMode) {
              e.preventDefault();
              return;
            }
            e.dataTransfer.setData('text/kiki-message-id', msg.id);
            e.dataTransfer.effectAllowed = 'move';

            // Use a compact floating pill as drag preview so it won't obscure sidebar topics
            const previewText = msg.content.replace(/\s+/g, ' ').trim();
            const label = `${isUser ? userProfile.name : 'Kiki'}：${previewText || '（空消息）'}`;
            const ghost = document.createElement('div');
            ghost.textContent = label;
            Object.assign(ghost.style, {
              position: 'fixed',
              top: '-9999px',
              left: '-9999px',
              maxWidth: '220px',
              padding: '6px 12px',
              borderRadius: '9999px',
              background: '#2E2B28',
              color: '#FAF8F5',
              fontSize: '12px',
              lineHeight: '1.2',
              fontWeight: '500',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              boxShadow: '0 6px 16px rgba(0, 0, 0, 0.18)',
              pointerEvents: 'none',
              zIndex: '9999',
            });
            document.body.appendChild(ghost);
            e.dataTransfer.setDragImage(ghost, 12, 16);
            requestAnimationFrame(() => {
              ghost.remove();
            });
          };

          const handleRowSelectClick = (e: React.MouseEvent) => {
            if (!selectionMode || !isSelectable || !onToggleSelectMessage) return;
            e.preventDefault();
            e.stopPropagation();
            onToggleSelectMessage(msg.id, e.shiftKey);
          };

          const msgIsStreaming = Boolean(msg.streaming ?? msg.metadata?.streaming);
          const isQuotable =
            !isSystem &&
            !msg.metadata?.optimistic &&
            !(msgIsStreaming && !msg.content.trim()) &&
            Boolean(onQuoteMessage);
          const isHighlighted = highlightedMessageId === msg.id;
          const quotedSnapshot = getQuotedSnapshot(msg);

          const handleTouchStart = (e: React.TouchEvent) => {
            if (selectionMode || !isQuotable || !onQuoteMessage) return;
            if (e.touches.length !== 1) return;
            const touch = e.touches[0];
            touchStartPosRef.current = { x: touch.clientX, y: touch.clientY };
            longPressTriggeredRef.current = false;
            clearLongPressTimer();
            longPressTimerRef.current = window.setTimeout(() => {
              longPressTimerRef.current = null;
              longPressTriggeredRef.current = true;
              if (typeof navigator !== 'undefined' && 'vibrate' in navigator) {
                try {
                  navigator.vibrate(15);
                } catch {
                  // ignore unsupported vibrate
                }
              }
              try {
                window.getSelection()?.removeAllRanges();
              } catch {
                // ignore
              }
              onQuoteMessage(msg);
              window.setTimeout(() => {
                longPressTriggeredRef.current = false;
              }, 500);
            }, 450);
          };

          const handleTouchMove = (e: React.TouchEvent) => {
            if (!touchStartPosRef.current || e.touches.length !== 1) {
              clearLongPressTimer();
              return;
            }
            const touch = e.touches[0];
            const dx = Math.abs(touch.clientX - touchStartPosRef.current.x);
            const dy = Math.abs(touch.clientY - touchStartPosRef.current.y);
            if (dx > 10 || dy > 10) {
              clearLongPressTimer();
            }
          };

          const handleTouchEndOrCancel = () => {
            clearLongPressTimer();
          };

          const handleBubbleContextMenu = (e: React.MouseEvent) => {
            if (longPressTriggeredRef.current) {
              e.preventDefault();
            }
          };

          const renderQuotedBlock = (forUserBubble: boolean) => {
            if (!quotedSnapshot) return null;
            const quotedSender =
              quotedSnapshot.role === 'user'
                ? userProfile.name
                : quotedSnapshot.role === 'assistant'
                  ? 'Kiki'
                  : '系统';
            const canJump = Boolean(messagesById[quotedSnapshot.id]);
            const preview = quotedSnapshot.content.replace(/\s+/g, ' ').trim() || '（空消息）';
            return (
              <button
                type="button"
                onClick={(e) => {
                  if (selectionMode) return;
                  e.stopPropagation();
                  if (canJump) {
                    handleJumpToMessage(quotedSnapshot.id);
                  }
                }}
                title={canJump ? '点击定位到被引用的原消息' : '被引用的历史消息'}
                className={`mb-2 w-full text-left rounded-xl px-2.5 py-1.5 border-l-2 transition-colors ${
                  forUserBubble
                    ? 'bg-white/10 border-[#8FB3D1] text-[#E8E2D9] hover:bg-white/15'
                    : 'bg-[#F5F1EB] border-[#5B7A95] text-[#5A534C] hover:bg-[#EFE9E0]'
                } ${canJump && !selectionMode ? 'cursor-pointer' : 'cursor-default'}`}
              >
                <div className="flex items-center gap-1.5 text-[11px] font-medium opacity-85">
                  <CornerUpLeft className="w-3 h-3 shrink-0" />
                  <span className="truncate">{quotedSender}</span>
                </div>
                <div className="mt-0.5 text-xs opacity-85 line-clamp-2 break-words leading-snug">
                  {preview}
                </div>
              </button>
            );
          };

          const renderQuoteButton = () => {
            if (selectionMode || !isQuotable || !onQuoteMessage) return null;
            return (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onQuoteMessage(msg);
                }}
                title="引用此消息并归属到其所属话题"
                aria-label="引用此消息"
                className="hidden sm:inline-flex items-center gap-1 px-2 py-1 rounded-full text-[11px] font-medium text-[#7A7269] hover:text-[#3B5972] bg-white/90 hover:bg-[#EAF0F5] border border-[#E5DED4] hover:border-[#CFE0EC] shadow-sm opacity-0 group-hover:opacity-100 focus:opacity-100 transition-all shrink-0 self-end mb-0.5"
              >
                <CornerUpLeft className="w-3 h-3" />
                <span>引用</span>
              </button>
            );
          };

          const renderCheckbox = () => {
            if (!selectionMode || !isSelectable) return null;
            return (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onToggleSelectMessage?.(msg.id, e.shiftKey);
                }}
                aria-label={isSelected ? '取消选中消息' : '选中消息'}
                className={`mt-2 w-5 h-5 rounded-md border flex items-center justify-center shrink-0 transition-colors ${
                  isSelected
                    ? 'bg-[#5B7A95] border-[#5B7A95] text-white'
                    : 'bg-white border-[#CFC6B9] hover:border-[#5B7A95]'
                }`}
              >
                {isSelected && <Check className="w-3.5 h-3.5" />}
              </button>
            );
          };

          if (isSystem) {
            return (
              <div
                key={msg.id}
                className="flex justify-center animate-kiki-fade-up"
              >
                <div className="px-3.5 py-1.5 rounded-full bg-[#F2EDE6] border border-[#E6DFD5] text-xs text-[#6F6860] max-w-lg text-center">
                  {msg.content}
                </div>
              </div>
            );
          }

          if (isUser) {
            return (
              <div
                key={msg.id}
                ref={(el) => {
                  messageRowRefs.current[msg.id] = el;
                }}
                onClick={selectionMode ? handleRowSelectClick : undefined}
                className={`group flex items-start justify-end gap-2.5 animate-kiki-fade-up rounded-2xl transition-all ${
                  selectionMode && isSelectable ? 'cursor-pointer p-1.5 -m-1.5' : ''
                } ${isSelected ? 'bg-[#EAF0F5]/65 ring-1 ring-[#5B7A95]/30' : ''} ${
                  isHighlighted ? 'bg-[#EAF0F5]/80 ring-2 ring-[#5B7A95]/45 p-1.5 -m-1.5' : ''
                }`}
              >
                {renderCheckbox()}

                <div className="flex-1 min-w-0 flex flex-col items-end">
                  <div className="flex items-center gap-2 mb-1.5">
                    {!selectionMode && (
                      <div
                        draggable
                        onDragStart={handleDragStart}
                        title="可拖拽此消息到左侧任意话题进行改绑"
                        className="inline-flex items-center justify-center p-1 -m-1 rounded opacity-0 group-hover:opacity-60 hover:!opacity-100 transition-opacity cursor-grab active:cursor-grabbing text-[#8C8378] [-webkit-user-drag:element]"
                      >
                        <GripVertical className="w-3.5 h-3.5 pointer-events-none" />
                      </div>
                    )}
                    {topic ? (
                      <div
                        className={
                          showAllTopicTags
                            ? 'opacity-100'
                            : 'opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity'
                        }
                      >
                        <TopicBadge
                          topic={topic}
                          messageId={msg.id}
                          confidence={confidence}
                          allTopics={allTopics}
                          onSelectTopic={onSelectTopic}
                          onReassign={onReassignMessage}
                          onStartMultiSelect={onStartMultiSelect}
                          compact
                        />
                      </div>
                    ) : msg.metadata?.optimistic ? (
                      <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] bg-[#F1ECE5] border border-[#E5DED4] text-[#7A7269]">
                        <Loader2 className="w-3 h-3 text-[#5B7A95] animate-spin" />
                        <span>归类路由中...</span>
                      </span>
                    ) : null}
                    <span className="text-[11px] text-[#9A9187]">
                      {formatTime(msg.created_at)}
                    </span>
                    <span className="text-xs font-semibold text-[#262422]">
                      {userProfile.name}
                    </span>
                  </div>

                  <div className="w-full flex items-end justify-end gap-1.5">
                    {renderQuoteButton()}
                    <div
                      onTouchStart={handleTouchStart}
                      onTouchMove={handleTouchMove}
                      onTouchEnd={handleTouchEndOrCancel}
                      onTouchCancel={handleTouchEndOrCancel}
                      onContextMenu={handleBubbleContextMenu}
                      className="max-w-[85%] sm:max-w-[78%] rounded-2xl rounded-tr-sm bg-[#34312E] text-[#FAF8F5] px-4 py-3 text-sm leading-relaxed shadow-sm whitespace-pre-wrap break-words"
                    >
                      {renderQuotedBlock(true)}
                      {msg.content}
                    </div>
                  </div>

                  {sessionRun && (
                    <div className="w-full max-w-[92%] sm:max-w-[85%]">
                      <RunStatusCard
                        sessionRun={sessionRun}
                        animateAvatar={shouldAnimateRun}
                        onOpenLogDrawer={onOpenLogDrawer}
                        onResumeRun={onResumeRun}
                        onCancelRun={onCancelRun}
                        onRetryRun={onRetryRun}
                        onPreviewArtifact={onPreviewArtifact}
                        onOpenDesktopTakeover={onOpenDesktopTakeover}
                      />
                    </div>
                  )}
                </div>

                {/* Customer's own avatar */}
                <div className="shrink-0 mt-1">
                  <UserAvatar size="md" editable={!selectionMode} />
                </div>
              </div>
            );
          }

          // Assistant message:
          // Completed messages stay completely static (`animated={false}`);
          // Only the actively streaming message animates (`thinking`).
          const isActivelyStreaming = Boolean(msgIsStreaming && msg.id === latestStreamingMsgId);
          const isInterimHeavyMsg = msg.metadata?.source === 'heavy_agent_interim';
          const inlineImageArtifacts =
            !isInterimHeavyMsg && candidateRun?.artifacts?.length
              ? candidateRun.artifacts.filter((art) =>
                  isImageArtifact(art.file_type, art.file_path)
                )
              : [];

          return (
            <div
              key={msg.id}
              ref={(el) => {
                messageRowRefs.current[msg.id] = el;
              }}
              onClick={selectionMode ? handleRowSelectClick : undefined}
              className={`group flex items-start gap-2.5 animate-kiki-fade-up rounded-2xl transition-all ${
                selectionMode && isSelectable ? 'cursor-pointer p-1.5 -m-1.5' : ''
              } ${isSelected ? 'bg-[#EAF0F5]/65 ring-1 ring-[#5B7A95]/30' : ''} ${
                isHighlighted ? 'bg-[#EAF0F5]/80 ring-2 ring-[#5B7A95]/45 p-1.5 -m-1.5' : ''
              }`}
            >
              {renderCheckbox()}

              <div className="shrink-0 mt-0.5">
                <KikiAvatar
                  status={isActivelyStreaming ? 'thinking' : 'idle'}
                  animated={isActivelyStreaming}
                  size="md"
                />
              </div>

              <div className="flex-1 min-w-0 max-w-[90%] sm:max-w-[85%]">
                <div className="flex items-center justify-between gap-2 mb-1.5">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-semibold text-[#262422]">
                      Kiki
                    </span>
                    <span className="text-[11px] text-[#9A9187]">
                      {formatTime(msg.created_at)}
                    </span>
                    {!selectionMode && (
                      <div
                        draggable
                        onDragStart={handleDragStart}
                        title="可拖拽此消息到左侧任意话题进行改绑"
                        className="inline-flex items-center justify-center p-1 -m-1 rounded opacity-0 group-hover:opacity-60 hover:!opacity-100 transition-opacity cursor-grab active:cursor-grabbing text-[#8C8378] [-webkit-user-drag:element]"
                      >
                        <GripVertical className="w-3.5 h-3.5 pointer-events-none" />
                      </div>
                    )}
                  </div>

                  {topic ? (
                    <div
                      className={
                        showAllTopicTags
                          ? 'opacity-100'
                          : 'opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity'
                      }
                    >
                      <TopicBadge
                        topic={topic}
                        messageId={msg.id}
                        confidence={confidence}
                        allTopics={allTopics}
                        onSelectTopic={onSelectTopic}
                        onReassign={onReassignMessage}
                        onStartMultiSelect={onStartMultiSelect}
                        compact
                      />
                    </div>
                  ) : null}
                </div>

                <div className="flex items-end gap-1.5">
                  <div
                    onTouchStart={handleTouchStart}
                    onTouchMove={handleTouchMove}
                    onTouchEnd={handleTouchEndOrCancel}
                    onTouchCancel={handleTouchEndOrCancel}
                    onContextMenu={handleBubbleContextMenu}
                    className="flex-1 min-w-0 rounded-2xl rounded-tl-sm bg-white border border-[#E8E2D9] px-4 py-3 text-sm text-[#262422] leading-relaxed shadow-sm shadow-stone-900/[0.02] whitespace-pre-wrap break-words"
                  >
                    {renderQuotedBlock(false)}
                    {renderMarkdownContent(msg.content)}
                  {inlineImageArtifacts.length > 0 && candidateRun && (
                    <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                      {inlineImageArtifacts.map((art, artIdx) => {
                        const artUrl = getArtifactUrl(candidateRun.id, art.id);
                        const fileName = art.file_path.split('/').pop() || art.file_path;
                        const previewItems: ArtifactPreviewItem[] = inlineImageArtifacts.map(
                          (artifact) => ({ runId: candidateRun.id, artifact })
                        );
                        return (
                          <div
                            key={art.id}
                            className="rounded-xl overflow-hidden border border-[#E8E2D9] bg-[#FAF8F5]"
                          >
                            <button
                              type="button"
                              onClick={(e) => {
                                if (selectionMode) return;
                                e.stopPropagation();
                                onPreviewArtifact?.(previewItems, artIdx);
                              }}
                              title="点击放大预览"
                              className="group/img relative block w-full bg-white cursor-zoom-in focus:outline-none focus-visible:ring-2 focus-visible:ring-[#5B7A95]"
                            >
                              <img
                                src={artUrl}
                                alt={art.description || fileName}
                                className="w-full max-h-80 object-contain bg-white"
                                loading="lazy"
                              />
                              <span className="pointer-events-none absolute bottom-2 right-2 inline-flex items-center gap-1 px-2 py-1 rounded-lg bg-[#1F1E1C]/75 text-[11px] font-medium text-[#FAF8F5] opacity-0 group-hover/img:opacity-100 transition-opacity backdrop-blur-sm shadow-sm">
                                <ZoomIn className="w-3 h-3" />
                                <span>预览</span>
                              </span>
                            </button>
                            <div className="px-2.5 py-1.5 flex items-center justify-between gap-2 text-[11px] text-[#7A7269] border-t border-[#EFEAE2]">
                              <span className="truncate" title={art.description || fileName}>
                                {art.description || fileName}
                              </span>
                              <a
                                href={`${artUrl}?download=true`}
                                download
                                onClick={(e) => e.stopPropagation()}
                                className="shrink-0 text-[#5B7A95] hover:underline font-medium"
                              >
                                下载
                              </a>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                  {msgIsStreaming && (
                    <span className="inline-flex items-center gap-1 ml-1.5 align-middle">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#5B7A95] animate-bounce [animation-delay:0ms]" />
                      <span className="w-1.5 h-1.5 rounded-full bg-[#5B7A95] animate-bounce [animation-delay:150ms]" />
                      <span className="w-1.5 h-1.5 rounded-full bg-[#5B7A95] animate-bounce [animation-delay:300ms]" />
                    </span>
                  )}
                  </div>
                  {renderQuoteButton()}
                </div>

                {sessionRun && (
                  <RunStatusCard
                    sessionRun={sessionRun}
                    animateAvatar={shouldAnimateRun}
                    onOpenLogDrawer={onOpenLogDrawer}
                    onResumeRun={onResumeRun}
                    onCancelRun={onCancelRun}
                    onRetryRun={onRetryRun}
                    onPreviewArtifact={onPreviewArtifact}
                    onOpenDesktopTakeover={onOpenDesktopTakeover}
                  />
                )}
              </div>
            </div>
          );
        })}

        {sending && (
          <div className="flex items-start gap-2.5 animate-kiki-fade-up">
            <div className="shrink-0 mt-0.5">
              <KikiAvatar status="running" pose="delivering" animated size="md" />
            </div>
            <div className="mt-2 rounded-2xl rounded-tl-sm bg-white/85 border border-[#E8E2D9] px-4 py-2.5 text-xs text-[#7A7269] inline-flex items-center gap-2 shadow-sm">
              <span>
                {pendingSendCount > 1
                  ? `Kiki 正在并行递信并分流 ${pendingSendCount} 条消息`
                  : 'Kiki 正在飞奔递信并分流话题'}
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-[#5B7A95] animate-bounce [animation-delay:0ms]" />
                <span className="w-1.5 h-1.5 rounded-full bg-[#5B7A95] animate-bounce [animation-delay:150ms]" />
                <span className="w-1.5 h-1.5 rounded-full bg-[#5B7A95] animate-bounce [animation-delay:300ms]" />
              </span>
            </div>
          </div>
        )}

        <div ref={bottomRef} className="h-2" />
      </div>
      </div>

      {showScrollToBottom && (
        <button
          type="button"
          onClick={handleJumpToBottom}
          aria-label="回到底部最新消息"
          className="absolute bottom-4 right-5 sm:right-8 z-20 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-white/95 hover:bg-[#FAF8F5] border border-[#DFD7CB] text-xs font-medium text-[#4A443E] shadow-md shadow-stone-900/10 backdrop-blur-sm transition-all"
        >
          <ArrowDown className="w-3.5 h-3.5 text-[#5B7A95]" />
          <span>回到底部</span>
        </button>
      )}
    </div>
  );
};

export default UnifiedMessageList;
