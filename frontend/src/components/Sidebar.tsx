import React, { useState } from 'react';
import { Archive, Check, Hash, Layers, Loader2, PawPrint, Pencil, Plus, X } from 'lucide-react';
import type { KikiStatus, ReassignTopicPayload, Topic } from '../types';
import KikiAvatar, { KIKI_POSE_LABELS, KIKI_STATUS_LABELS, type KikiPose } from './KikiAvatar';
import UserAvatar from './UserAvatar';

export interface SidebarProps {
  topics: Topic[];
  focusTopicId: string | null;
  globalKikiStatus: KikiStatus;
  companionPose?: KikiPose;
  companionCaption?: string | null;
  sseConnected: boolean;
  onSelectAllStream: () => void;
  onSelectTopic: (topicId: string) => void;
  onCreateTopic: (title: string) => Promise<void> | void;
  onRenameTopic?: (topicId: string, title: string) => Promise<void> | void;
  onArchiveTopic?: (topicId: string) => Promise<void> | void;
  onReassignMessage: (
    messageId: string,
    payload: ReassignTopicPayload
  ) => Promise<void> | void;
}

export const Sidebar: React.FC<SidebarProps> = ({
  topics,
  focusTopicId,
  globalKikiStatus,
  companionPose,
  companionCaption = null,
  sseConnected,
  onSelectAllStream,
  onSelectTopic,
  onCreateTopic,
  onRenameTopic,
  onArchiveTopic,
  onReassignMessage,
}) => {
  const [dragOverTopicId, setDragOverTopicId] = useState<string | null>(null);
  const [addingTopic, setAddingTopic] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [creating, setCreating] = useState(false);
  const [editingTopicId, setEditingTopicId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState('');
  const [companionMoodLabel, setCompanionMoodLabel] = useState<string | null>(null);

  const activeTopics = topics.filter((t) => t.status === 'active');

  const isPureIdle = globalKikiStatus === 'idle' && (!companionPose || companionPose === 'idle');

  const statusBadgeText = companionCaption
    ? companionCaption
    : isPureIdle && companionMoodLabel
      ? companionMoodLabel
      : companionPose
        ? KIKI_POSE_LABELS[companionPose]
        : KIKI_STATUS_LABELS[globalKikiStatus];

  const handleDrop = async (e: React.DragEvent, targetTopicId: string) => {
    e.preventDefault();
    setDragOverTopicId(null);
    const messageId = e.dataTransfer.getData('text/kiki-message-id');
    if (!messageId) return;
    await onReassignMessage(messageId, {
      action: 'move',
      target_topic_id: targetTopicId,
    });
  };

  const handleCreateSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = newTitle.trim();
    if (!trimmed || creating) return;
    setCreating(true);
    try {
      await onCreateTopic(trimmed);
      setNewTitle('');
      setAddingTopic(false);
    } finally {
      setCreating(false);
    }
  };

  return (
    <aside className="w-64 lg:w-72 shrink-0 h-full bg-[#F5F1EB] border-r border-[#E6DFD5] flex flex-col select-none">
      {/* Brand Header (Static brand emblem) + Customer Profile Avatar */}
      <div className="px-4 py-3.5 border-b border-[#E8E2D9] flex items-center justify-between gap-2">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="w-10 h-10 rounded-2xl bg-[#FAF8F5] border border-[#E2D9CC] shadow-sm flex items-center justify-center shrink-0 overflow-hidden">
            <KikiAvatar status="idle" animated={false} size="sm" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <h1 className="text-base font-bold text-[#262422] tracking-tight">
                Kiki
              </h1>
              <span
                title={sseConnected ? '实时事件流已连接' : '事件流重连中'}
                className={`w-2 h-2 rounded-full ${
                  sseConnected ? 'bg-emerald-500' : 'bg-amber-400 animate-pulse'
                }`}
              />
            </div>
            <p className="text-[11px] text-[#7D756C] truncate">Personal Agent</p>
          </div>
        </div>

        {/* Customer's own avatar (clickable to customize photo & display name) */}
        <UserAvatar size="sm" editable />
      </div>

      {/* Primary Navigation */}
      <div className="px-3 pt-3 pb-2">
        <button
          type="button"
          onClick={onSelectAllStream}
          className={`w-full flex items-center justify-between px-3 py-2.5 rounded-xl text-sm font-medium transition-all ${
            focusTopicId === null
              ? 'bg-white text-[#262422] shadow-sm border border-[#E5DED4]'
              : 'text-[#5A534C] hover:bg-[#ECE6DD] border border-transparent'
          }`}
        >
          <span className="flex items-center gap-2.5">
            <Layers
              className={`w-4 h-4 ${
                focusTopicId === null ? 'text-[#5B7A95]' : 'text-[#8C8378]'
              }`}
            />
            <span>All Stream</span>
            <span className="text-[11px] text-[#8C8378] font-normal">全局流</span>
          </span>
        </button>
      </div>

      {/* Active Topics List */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-1">
        <div className="px-2 pb-1.5 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-[#8C8378]">
            活跃话题 ({activeTopics.length})
          </span>
          <button
            type="button"
            onClick={() => setAddingTopic((prev) => !prev)}
            title="新建话题"
            className="p-1 rounded-md text-[#7D756C] hover:text-[#262422] hover:bg-[#E9E2D8] transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
          </button>
        </div>

        {addingTopic && (
          <form onSubmit={handleCreateSubmit} className="mb-2 px-1">
            <input
              type="text"
              autoFocus
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              onBlur={() => {
                if (!newTitle.trim()) setAddingTopic(false);
              }}
              placeholder="输入话题名，回车创建..."
              className="w-full px-2.5 py-1.5 text-xs rounded-xl bg-white border border-[#5B7A95] focus:outline-none shadow-sm"
            />
          </form>
        )}

        {activeTopics.length === 0 ? (
          <div className="px-3 py-6 text-center text-xs text-[#9A9187] leading-relaxed">
            发送消息后，Kiki 会自动为你归类生成话题；也可以将消息拖拽到此处改绑。
          </div>
        ) : (
          activeTopics.map((topic) => {
            const isSelected = focusTopicId === topic.id;
            const isDragTarget = dragOverTopicId === topic.id;
            const isRunning = (topic.active_run_count ?? 0) > 0;
            const isGeneral = topic.id === 'topic-general';
            const isEditing = editingTopicId === topic.id;

            if (isEditing) {
              return (
                <form
                  key={topic.id}
                  onSubmit={async (e) => {
                    e.preventDefault();
                    const trimmed = editingTitle.trim();
                    if (trimmed && trimmed !== topic.title) {
                      await onRenameTopic?.(topic.id, trimmed);
                    }
                    setEditingTopicId(null);
                  }}
                  className="flex items-center gap-1 px-2 py-1.5 rounded-xl bg-white border border-[#5B7A95] shadow-sm"
                >
                  <input
                    type="text"
                    autoFocus
                    value={editingTitle}
                    onChange={(e) => setEditingTitle(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') setEditingTopicId(null);
                    }}
                    className="flex-1 min-w-0 text-xs bg-transparent focus:outline-none text-[#262422]"
                  />
                  <button
                    type="submit"
                    aria-label="保存话题名"
                    className="p-0.5 rounded text-[#5B7A95] hover:bg-[#EAF0F5]"
                  >
                    <Check className="w-3.5 h-3.5" />
                  </button>
                  <button
                    type="button"
                    aria-label="取消重命名"
                    onClick={() => setEditingTopicId(null)}
                    className="p-0.5 rounded text-[#8C8378] hover:bg-[#F3EFE9]"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </form>
              );
            }

            return (
              <div
                key={topic.id}
                role="button"
                tabIndex={0}
                title={topic.summary || topic.title}
                onClick={() => onSelectTopic(topic.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onSelectTopic(topic.id);
                  }
                }}
                onDragOver={(e) => {
                  e.preventDefault();
                  e.dataTransfer.dropEffect = 'move';
                  if (dragOverTopicId !== topic.id) {
                    setDragOverTopicId(topic.id);
                  }
                }}
                onDragLeave={() => {
                  if (dragOverTopicId === topic.id) {
                    setDragOverTopicId(null);
                  }
                }}
                onDrop={(e) => handleDrop(e, topic.id)}
                className={`w-full group flex items-center justify-between gap-2 px-3 py-2 rounded-xl text-left text-xs transition-all border cursor-pointer ${
                  isDragTarget
                    ? 'bg-amber-50 border-amber-400 ring-2 ring-amber-300/60 scale-[1.01]'
                    : isSelected
                      ? 'bg-white text-[#262422] font-semibold border-[#E5DED4] shadow-sm'
                      : 'text-[#4E4842] hover:bg-[#ECE6DD] border-transparent'
                }`}
              >
                <div className="flex items-start gap-2 min-w-0 flex-1">
                  <Hash
                    className={`w-3.5 h-3.5 shrink-0 mt-0.5 ${
                      isSelected ? 'text-[#5B7A95]' : 'text-[#9A9187]'
                    }`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="truncate">{topic.title}</div>
                    {topic.summary && (
                      <div className="text-[10px] font-normal text-[#8C8378] truncate mt-0.5">
                        {topic.summary}
                      </div>
                    )}
                  </div>
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  {!isGeneral && (onRenameTopic || onArchiveTopic) && (
                    <div className="hidden group-hover:flex items-center gap-0.5">
                      {onRenameTopic && (
                        <button
                          type="button"
                          title="重命名话题"
                          aria-label="重命名话题"
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingTopicId(topic.id);
                            setEditingTitle(topic.title);
                          }}
                          className="p-1 rounded-md text-[#7D756C] hover:text-[#262422] hover:bg-[#E2DBD0]"
                        >
                          <Pencil className="w-3 h-3" />
                        </button>
                      )}
                      {onArchiveTopic && (
                        <button
                          type="button"
                          title="归档话题"
                          aria-label="归档话题"
                          onClick={(e) => {
                            e.stopPropagation();
                            void onArchiveTopic(topic.id);
                          }}
                          className="p-1 rounded-md text-[#7D756C] hover:text-rose-700 hover:bg-rose-50"
                        >
                          <Archive className="w-3 h-3" />
                        </button>
                      )}
                    </div>
                  )}

                  {isRunning && (
                    <span title="后台任务执行中">
                      <Loader2 className="w-3.5 h-3.5 text-[#5B7A95] animate-spin" />
                    </span>
                  )}

                  {topic.unread && (
                    <span
                      title="有新的后台任务结果未读"
                      className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-[#E85D52] text-white shadow-sm"
                    >
                      <PawPrint className="w-2.5 h-2.5" />
                    </span>
                  )}

                  {typeof topic.message_count === 'number' && (
                    <span className="text-[10px] font-mono text-[#9A9187] tabular-nums group-hover:hidden">
                      {topic.message_count}
                    </span>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Bottom Pinned Kiki Companion Stage (Live animated companion) */}
      <div className="p-3 border-t border-[#E8E2D9]">
        <div
          className={`rounded-2xl border px-3 py-2.5 flex items-center gap-2.5 transition-colors ${
            globalKikiStatus === 'waiting_input'
              ? 'bg-amber-50/70 border-amber-300'
              : globalKikiStatus === 'running'
                ? 'bg-[#EEF4F8] border-[#C9DAE8]'
                : 'bg-[#FAF8F5] border-[#E6DFD5]'
          }`}
        >
          <KikiAvatar
            status={isPureIdle ? 'idle' : globalKikiStatus}
            pose={isPureIdle ? undefined : companionPose}
            animated
            companion={isPureIdle}
            size="xl"
            className="-my-1.5 -ml-1"
            onCompanionMoodChange={setCompanionMoodLabel}
          />
          <div className="min-w-0 flex-1">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[#8C8378]">
              Kiki 伴宠小窝
            </div>
            <div className="mt-1">
              <span
                className={`inline-block max-w-full truncate px-2 py-0.5 rounded-full text-[11px] font-medium ${
                  globalKikiStatus === 'waiting_input'
                    ? 'bg-amber-200/80 text-amber-900'
                    : globalKikiStatus === 'running'
                      ? 'bg-[#5B7A95]/15 text-[#3B5972]'
                      : globalKikiStatus === 'thinking'
                        ? 'bg-[#EAE4DC] text-[#4E4842]'
                        : companionMoodLabel
                          ? 'bg-[#E7EFF5] text-[#3B5972]'
                          : 'bg-[#ECE6DD] text-[#5A534C]'
                }`}
                title={statusBadgeText}
              >
                {statusBadgeText}
              </span>
            </div>
            <div className="mt-1 text-[10px] text-[#8C8378] truncate">
              {isPureIdle
                ? '点点 Kiki 可逗她 · 拖拽消息改绑'
                : '拖拽消息可快速改绑话题'}
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
};

export default Sidebar;
