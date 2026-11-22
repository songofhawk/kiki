import React, { useEffect, useRef } from 'react';
import { GripVertical, Sparkles, Terminal, ShieldAlert, MessageCircle } from 'lucide-react';
import type {
  Message,
  ReassignTopicPayload,
  ResumeRunPayload,
  SessionRun,
  Topic,
} from '../types';
import KikiAvatar from './KikiAvatar';
import TopicBadge from './TopicBadge';
import RunStatusCard from './RunStatusCard';

export interface UnifiedMessageListProps {
  messages: Message[];
  topicsById: Record<string, Topic>;
  allTopics: Topic[];
  runsById: Record<string, SessionRun>;
  focusTopicId: string | null;
  sending?: boolean;
  onSelectTopic: (topicId: string) => void;
  onReassignMessage: (
    messageId: string,
    payload: ReassignTopicPayload
  ) => Promise<void> | void;
  onOpenLogDrawer: (runId: string) => void;
  onResumeRun: (runId: string, payload: ResumeRunPayload) => Promise<void> | void;
  onCancelRun: (runId: string) => Promise<void> | void;
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
    return <React.Fragment key={`${keyPrefix}-t-${idx}`}>{part}</React.Fragment>;
  });
}

function renderInlineMarkdown(text: string): React.ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, idx) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return (
        <strong key={`b-${idx}`} className="font-semibold text-[#1E1C1A]">
          {renderInlineCode(part.slice(2, -2), `b-${idx}`)}
        </strong>
      );
    }
    return (
      <React.Fragment key={`p-${idx}`}>
        {renderInlineCode(part, `p-${idx}`)}
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
  sending = false,
  onSelectTopic,
  onReassignMessage,
  onOpenLogDrawer,
  onResumeRun,
  onCancelRun,
  onStarterPrompt,
}) => {
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages.length, focusTopicId]);

  if (messages.length === 0) {
    return (
      <div className="flex-1 overflow-y-auto px-4 sm:px-8 py-10 flex flex-col items-center justify-center">
        <div className="max-w-xl w-full text-center space-y-6 animate-kiki-fade-up">
          <div className="flex flex-col items-center gap-2">
            <KikiAvatar status="idle" size="hero" />
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
                    <p className="text-xs text-[#787067] line-clamp-3 leading-relaxed">
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

  const renderedRunIds = new Set<string>();

  return (
    <div className="flex-1 overflow-y-auto px-4 sm:px-6 lg:px-8 py-6">
      <div className="max-w-3xl mx-auto space-y-5">
        {messages.map((msg) => {
          const isUser = msg.role === 'user';
          const isSystem = msg.role === 'system';
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

          const handleDragStart = (e: React.DragEvent<HTMLDivElement>) => {
            e.dataTransfer.setData('text/kiki-message-id', msg.id);
            e.dataTransfer.effectAllowed = 'move';
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
                draggable
                onDragStart={handleDragStart}
                className="group flex flex-col items-end animate-kiki-fade-up"
              >
                <div className="flex items-center gap-2 mb-1.5">
                  <span
                    title="可拖拽此消息到左侧任意话题进行改绑"
                    className="opacity-0 group-hover:opacity-60 transition-opacity cursor-grab active:cursor-grabbing text-[#8C8378]"
                  >
                    <GripVertical className="w-3.5 h-3.5" />
                  </span>
                  <span className="text-[11px] text-[#9A9187]">
                    {formatTime(msg.created_at)}
                  </span>
                  {topic ? (
                    <TopicBadge
                      topic={topic}
                      messageId={msg.id}
                      confidence={confidence}
                      allTopics={allTopics}
                      onSelectTopic={onSelectTopic}
                      onReassign={onReassignMessage}
                      compact
                    />
                  ) : msg.metadata?.optimistic ? (
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] bg-[#F1ECE5] border border-[#E5DED4] text-[#7A7269] animate-pulse">
                      <KikiAvatar status="thinking" size="xs" />
                      <span>Kiki 归类中...</span>
                    </span>
                  ) : null}
                </div>

                <div className="max-w-[85%] sm:max-w-[78%] rounded-2xl rounded-tr-sm bg-[#34312E] text-[#FAF8F5] px-4 py-3 text-sm leading-relaxed shadow-sm whitespace-pre-wrap break-words">
                  {msg.content}
                </div>

                {/* Heavy-path RunStatusCard if attached directly to the user trigger message */}
                {sessionRun && (
                  <div className="w-full max-w-[92%] sm:max-w-[85%]">
                    <RunStatusCard
                      sessionRun={sessionRun}
                      onOpenLogDrawer={onOpenLogDrawer}
                      onResumeRun={onResumeRun}
                      onCancelRun={onCancelRun}
                    />
                  </div>
                )}
              </div>
            );
          }

          // Assistant message
          return (
            <div
              key={msg.id}
              draggable
              onDragStart={handleDragStart}
              className="group flex items-start gap-2.5 animate-kiki-fade-up"
            >
              <div className="shrink-0 mt-0.5">
                <KikiAvatar
                  status={msg.streaming ? 'thinking' : 'idle'}
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
                    <span
                      title="可拖拽此消息到左侧任意话题进行改绑"
                      className="opacity-0 group-hover:opacity-60 transition-opacity cursor-grab active:cursor-grabbing text-[#8C8378]"
                    >
                      <GripVertical className="w-3.5 h-3.5" />
                    </span>
                  </div>

                  {topic ? (
                    <TopicBadge
                      topic={topic}
                      messageId={msg.id}
                      confidence={confidence}
                      allTopics={allTopics}
                      onSelectTopic={onSelectTopic}
                      onReassign={onReassignMessage}
                      compact
                    />
                  ) : msg.metadata?.optimistic ? (
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] bg-[#F1ECE5] border border-[#E5DED4] text-[#7A7269] animate-pulse">
                      <KikiAvatar status="thinking" size="xs" />
                      <span>Kiki 归类中...</span>
                    </span>
                  ) : null}
                </div>

                <div className="rounded-2xl rounded-tl-sm bg-white border border-[#E8E2D9] px-4 py-3 text-sm text-[#262422] leading-relaxed shadow-sm shadow-stone-900/[0.02] whitespace-pre-wrap break-words">
                  {renderInlineMarkdown(msg.content)}
                  {msg.streaming && (
                    <span className="inline-flex items-center gap-1 ml-1.5 align-middle">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#5B7A95] animate-bounce [animation-delay:0ms]" />
                      <span className="w-1.5 h-1.5 rounded-full bg-[#5B7A95] animate-bounce [animation-delay:150ms]" />
                      <span className="w-1.5 h-1.5 rounded-full bg-[#5B7A95] animate-bounce [animation-delay:300ms]" />
                    </span>
                  )}
                </div>

                {sessionRun && (
                  <RunStatusCard
                    sessionRun={sessionRun}
                    onOpenLogDrawer={onOpenLogDrawer}
                    onResumeRun={onResumeRun}
                    onCancelRun={onCancelRun}
                  />
                )}
              </div>
            </div>
          );
        })}

        {sending && (
          <div className="flex items-start gap-2.5 animate-kiki-fade-up">
            <div className="shrink-0 mt-0.5">
              <KikiAvatar status="thinking" size="md" />
            </div>
            <div className="mt-2 rounded-2xl rounded-tl-sm bg-white/80 border border-[#E8E2D9] px-4 py-2.5 text-xs text-[#7A7269] inline-flex items-center gap-2 shadow-sm">
              <span>Kiki 正在思考并分流话题</span>
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
  );
};

export default UnifiedMessageList;
