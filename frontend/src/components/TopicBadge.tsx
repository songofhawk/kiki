import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChevronDown, FolderInput, GitBranchPlus, Check } from 'lucide-react';
import type { ReassignTopicPayload, Topic } from '../types';
import KikiAvatar from './KikiAvatar';

export interface TopicBadgeProps {
  topic: Topic;
  messageId?: string;
  confidence?: number;
  allTopics?: Topic[];
  onSelectTopic?: (topicId: string) => void;
  onReassign?: (messageId: string, payload: ReassignTopicPayload) => Promise<void> | void;
  compact?: boolean;
}

const EDITORIAL_PALETTE = [
  'bg-[#EAF0F5] text-[#3B5972] border-[#C9DAE8] hover:bg-[#DFEAF2]',
  'bg-[#ECF3EC] text-[#3B6244] border-[#CBE0CD] hover:bg-[#E1EDE2]',
  'bg-[#F7EFE6] text-[#8A5628] border-[#E7D3BC] hover:bg-[#F1E4D5]',
  'bg-[#F5EBF0] text-[#7D415E] border-[#E5CCD8] hover:bg-[#EEDEE7]',
  'bg-[#EFEBF6] text-[#55447D] border-[#D7CEE8] hover:bg-[#E5DFF1]',
  'bg-[#EAF4F3] text-[#2F6461] border-[#C4E1DE] hover:bg-[#DDECEB]',
  'bg-[#F5F2E4] text-[#746227] border-[#E3DCB8] hover:bg-[#ECE7D0]',
];

export function getTopicColorClasses(topicId: string): string {
  let hash = 0;
  for (let i = 0; i < topicId.length; i++) {
    hash = (hash * 31 + topicId.charCodeAt(i)) >>> 0;
  }
  return EDITORIAL_PALETTE[hash % EDITORIAL_PALETTE.length];
}

export const TopicBadge: React.FC<TopicBadgeProps> = ({
  topic,
  messageId,
  confidence,
  allTopics = [],
  onSelectTopic,
  onReassign,
  compact = false,
}) => {
  const [menuOpen, setMenuOpen] = useState(false);
  const [showSplitInput, setShowSplitInput] = useState(false);
  const [splitTitle, setSplitTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const [menuPos, setMenuPos] = useState<{ top: number; left: number } | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const isLowConfidence = confidence !== undefined && confidence < 0.65;
  const canReassign = Boolean(messageId && onReassign);
  const otherTopics = allTopics.filter(
    (t) => t.id !== topic.id && t.status === 'active'
  );

  useLayoutEffect(() => {
    if (!menuOpen || !containerRef.current) {
      setMenuPos(null);
      return;
    }
    const updatePosition = () => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const menuWidth = 240;
      const left = Math.max(
        12,
        Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 12)
      );
      const top = Math.min(rect.bottom + 6, window.innerHeight - 260);
      setMenuPos({ top, left });
    };
    updatePosition();
    window.addEventListener('scroll', updatePosition, true);
    window.addEventListener('resize', updatePosition);
    return () => {
      window.removeEventListener('scroll', updatePosition, true);
      window.removeEventListener('resize', updatePosition);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (!menuOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as Node;
      const insideTrigger = containerRef.current?.contains(target);
      const insideMenu = menuRef.current?.contains(target);
      if (!insideTrigger && !insideMenu) {
        setMenuOpen(false);
        setShowSplitInput(false);
      }
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setMenuOpen(false);
        setShowSplitInput(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKey);
    };
  }, [menuOpen]);

  const handleSplit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!messageId || !onReassign || busy) return;
    setBusy(true);
    try {
      await onReassign(messageId, {
        action: 'split',
        new_topic_title: splitTitle.trim() || undefined,
      });
      setMenuOpen(false);
      setShowSplitInput(false);
      setSplitTitle('');
    } finally {
      setBusy(false);
    }
  };

  const handleMove = async (targetTopicId: string) => {
    if (!messageId || !onReassign || busy) return;
    setBusy(true);
    try {
      await onReassign(messageId, {
        action: 'move',
        target_topic_id: targetTopicId,
      });
      setMenuOpen(false);
    } finally {
      setBusy(false);
    }
  };

  const colorClasses = getTopicColorClasses(topic.id);

  return (
    <div
      ref={containerRef}
      draggable={false}
      onDragStart={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
      className="relative inline-flex items-center gap-1 group"
    >
      {isLowConfidence && canReassign && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setMenuOpen((prev) => !prev);
          }}
          title={`路由置信度 ${Math.round((confidence ?? 0) * 100)}%，点击可快速改绑或拆为新话题`}
          className="inline-flex items-center gap-0.5 rounded-full bg-amber-50/90 border border-amber-300/80 pl-0.5 pr-1.5 py-0.5 text-[10px] text-amber-800 hover:bg-amber-100/80 transition-colors"
        >
          <KikiAvatar status="thinking" size="xs" />
          <span className="font-medium">不确定?</span>
        </button>
      )}

      <div
        className={`inline-flex items-center rounded-full border transition-colors ${colorClasses} ${
          compact ? 'text-[11px]' : 'text-xs'
        }`}
      >
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onSelectTopic?.(topic.id);
          }}
          title={`进入话题专注模式：#${topic.title}`}
          className={`font-medium truncate max-w-[160px] ${
            canReassign ? 'pl-2.5 pr-1 py-0.5' : 'px-2.5 py-0.5'
          }`}
        >
          #{topic.title}
        </button>

        {canReassign && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setMenuOpen((prev) => !prev);
            }}
            aria-label="调整消息所属话题"
            title="拆为新话题或移至其他话题"
            className={`pr-1.5 pl-0.5 py-0.5 rounded-r-full transition-opacity ${
              isLowConfidence || menuOpen
                ? 'opacity-90'
                : 'opacity-40 group-hover:opacity-100'
            }`}
          >
            <ChevronDown className="w-3 h-3" />
          </button>
        )}
      </div>

      {menuOpen &&
        canReassign &&
        menuPos &&
        createPortal(
          <div
            ref={menuRef}
            style={{ top: menuPos.top, left: menuPos.left }}
            onClick={(e) => e.stopPropagation()}
            className="fixed z-[100] w-60 rounded-xl bg-white border border-[#E8E2D9] shadow-xl shadow-stone-900/10 p-1.5 text-left"
          >
            {isLowConfidence && (
              <div className="px-2.5 py-1.5 mb-1 rounded-lg bg-amber-50/70 border border-amber-200/70 flex items-center gap-2">
                <KikiAvatar status="thinking" size="xs" />
                <div className="text-[11px] leading-tight text-amber-900">
                  Kiki 路由置信度较低 ({Math.round((confidence ?? 0) * 100)}%)，需要帮你改绑吗？
                </div>
              </div>
            )}

            {!showSplitInput ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => setShowSplitInput(true)}
                className="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs font-medium text-[#262422] hover:bg-[#FAF8F5] transition-colors disabled:opacity-50"
              >
                <GitBranchPlus className="w-3.5 h-3.5 text-[#5B7A95]" />
                <span>拆为新话题</span>
              </button>
            ) : (
              <form onSubmit={handleSplit} className="p-1.5 space-y-1.5">
                <input
                  id={`split-topic-${messageId ?? 'input'}`}
                  name="new_topic_title"
                  type="text"
                  autoFocus
                  value={splitTitle}
                  onChange={(e) => setSplitTitle(e.target.value)}
                  placeholder="新话题标题（留空自动命名）"
                  className="w-full px-2.5 py-1.5 text-xs rounded-lg border border-[#E8E2D9] bg-[#FAF8F5] focus:outline-none focus:border-[#5B7A95]"
                />
                <div className="flex items-center justify-end gap-1.5">
                  <button
                    type="button"
                    onClick={() => {
                      setShowSplitInput(false);
                      setSplitTitle('');
                    }}
                    className="px-2 py-1 text-[11px] text-[#7A7269] hover:text-[#262422]"
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    disabled={busy}
                    className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] font-medium bg-[#5B7A95] text-white hover:bg-[#4A6882] disabled:opacity-50"
                  >
                    <Check className="w-3 h-3" />
                    确认拆分
                  </button>
                </div>
              </form>
            )}

            <div className="my-1 border-t border-[#F0EAE1]" />

            <div className="px-2.5 py-1 text-[10px] font-medium uppercase tracking-wider text-[#8C8378] flex items-center gap-1.5">
              <FolderInput className="w-3 h-3" />
              移至其他话题...
            </div>

            {otherTopics.length === 0 ? (
              <div className="px-2.5 py-2 text-[11px] text-[#9A9187]">
                暂无其他活跃话题
              </div>
            ) : (
              <div className="max-h-40 overflow-y-auto space-y-0.5">
                {otherTopics.map((other) => (
                  <button
                    key={other.id}
                    type="button"
                    disabled={busy}
                    onClick={(e) => {
                      e.stopPropagation();
                      void handleMove(other.id);
                    }}
                    className="w-full text-left px-2.5 py-1.5 rounded-lg text-xs text-[#3A3530] hover:bg-[#FAF8F5] truncate transition-colors disabled:opacity-50"
                  >
                    #{other.title}
                  </button>
                ))}
              </div>
            )}
          </div>,
          document.body
        )}
    </div>
  );
};

export default TopicBadge;
