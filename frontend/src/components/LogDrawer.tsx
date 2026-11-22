import React, { useEffect, useRef, useState } from 'react';
import { Check, Copy, Terminal, X } from 'lucide-react';
import type { RawLogLine, SessionRun } from '../types';

export interface LogDrawerProps {
  run: SessionRun | null;
  rawLogs: RawLogLine[];
  onClose: () => void;
}

const STATUS_BADGE: Record<
  SessionRun['status'],
  { label: string; classes: string }
> = {
  queued: { label: '排队中', classes: 'bg-stone-700 text-stone-200' },
  running: { label: '运行中', classes: 'bg-sky-500/20 text-sky-300 border border-sky-500/40' },
  waiting_input: {
    label: '等待拍板',
    classes: 'bg-amber-500/20 text-amber-300 border border-amber-500/40',
  },
  completed: {
    label: '已完成',
    classes: 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40',
  },
  failed: {
    label: '失败',
    classes: 'bg-rose-500/20 text-rose-300 border border-rose-500/40',
  },
  cancelled: {
    label: '已取消',
    classes: 'bg-stone-600/30 text-stone-300 border border-stone-500/40',
  },
};

export const LogDrawer: React.FC<LogDrawerProps> = ({ run, rawLogs, onClose }) => {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!run) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [run, onClose]);

  useEffect(() => {
    if (!scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [rawLogs.length, run?.id]);

  if (!run) return null;

  const badge = STATUS_BADGE[run.status] ?? STATUS_BADGE.running;
  const combinedLogs = rawLogs.length > 0 ? rawLogs : run.raw_logs ?? [];

  const handleCopy = async () => {
    const text = combinedLogs
      .map((l) => `[${l.timestamp}] [${l.stream}] ${l.line}`)
      .join('\n');
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard might be unavailable
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* Backdrop */}
      <div
        onClick={onClose}
        className="fixed inset-0 bg-[#262422]/25 backdrop-blur-[1px] transition-opacity"
      />

      {/* Slide-over drawer */}
      <aside className="relative z-10 w-full max-w-2xl h-full bg-[#1C1B19] text-[#EAE6DF] shadow-2xl border-l border-[#33302C] flex flex-col">
        {/* Header */}
        <div className="px-4 py-3 border-b border-[#2F2D2A] flex items-center justify-between gap-3 bg-[#22201E]">
          <div className="flex items-center gap-2.5 min-w-0">
            <div className="w-7 h-7 rounded-lg bg-[#2E2B28] border border-[#3E3A35] flex items-center justify-center text-[#93B4CE] shrink-0">
              <Terminal className="w-4 h-4" />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-[#F5F2EC] truncate">
                  L3 极客终端日志
                </span>
                <span
                  className={`px-2 py-0.5 rounded-full text-[10px] font-medium ${badge.classes}`}
                >
                  {badge.label}
                </span>
              </div>
              <div className="text-[11px] font-mono text-[#9A938A] truncate">
                {run.agent_type} · run:{run.id.slice(0, 12)}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-1.5 shrink-0">
            <button
              type="button"
              onClick={handleCopy}
              disabled={combinedLogs.length === 0}
              className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-[#2C2A27] hover:bg-[#383531] text-[#DDD6CC] border border-[#3D3934] transition-colors disabled:opacity-40"
            >
              {copied ? (
                <>
                  <Check className="w-3.5 h-3.5 text-emerald-400" />
                  已复制
                </>
              ) : (
                <>
                  <Copy className="w-3.5 h-3.5" />
                  复制
                </>
              )}
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭日志抽屉"
              className="p-1.5 rounded-lg text-[#A39C93] hover:text-white hover:bg-[#2E2B28] transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Sub-bar with current step */}
        {run.current_step && (
          <div className="px-4 py-2 bg-[#181715] border-b border-[#2B2926] text-xs font-mono text-[#B7B0A6] flex items-center gap-2">
            <span className="text-[#789AB5]">$</span>
            <span className="truncate">{run.current_step}</span>
          </div>
        )}

        {/* Terminal output */}
        <div
          ref={scrollRef}
          className="flex-1 overflow-y-auto p-4 font-mono text-xs leading-relaxed space-y-1 kiki-terminal-scroll"
        >
          {combinedLogs.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-[#78726B] space-y-2">
              <Terminal className="w-7 h-7 opacity-40" />
              <p className="text-xs">暂无原生标准输出，等待后台 Agent 推送日志...</p>
            </div>
          ) : (
            combinedLogs.map((entry, idx) => {
              const streamColor =
                entry.stream === 'stderr'
                  ? 'text-rose-400'
                  : entry.stream === 'ndjson'
                  ? 'text-sky-300'
                  : 'text-[#E5E0D8]';
              const timeStr = entry.timestamp
                ? entry.timestamp.includes('T')
                  ? entry.timestamp.split('T')[1]?.slice(0, 8)
                  : entry.timestamp.slice(0, 8)
                : '';

              return (
                <div
                  key={`${entry.timestamp}-${idx}`}
                  className="flex items-start gap-2.5 break-all hover:bg-white/[0.03] px-1.5 py-0.5 rounded"
                >
                  <span className="text-[#6F6962] shrink-0 select-none text-[11px]">
                    {timeStr}
                  </span>
                  <span
                    className={`shrink-0 uppercase text-[10px] px-1 rounded select-none ${
                      entry.stream === 'stderr'
                        ? 'bg-rose-950/80 text-rose-300'
                        : entry.stream === 'ndjson'
                        ? 'bg-sky-950/70 text-sky-300'
                        : 'bg-stone-800 text-stone-400'
                    }`}
                  >
                    {entry.stream}
                  </span>
                  <span className={`${streamColor} whitespace-pre-wrap flex-1`}>
                    {entry.line}
                  </span>
                </div>
              );
            })
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-[#2F2D2A] bg-[#22201E] text-[11px] text-[#8C857C] flex items-center justify-between">
          <span>共 {combinedLogs.length} 行输出</span>
          <span>按 ESC 关闭</span>
        </div>
      </aside>
    </div>
  );
};

export default LogDrawer;
