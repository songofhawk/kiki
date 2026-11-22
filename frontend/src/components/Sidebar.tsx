import React, { useState } from 'react';
import { Hash, Layers, Loader2, PawPrint, Plus } from 'lucide-react';
import type { KikiStatus, ReassignTopicPayload, Topic } from '../types';
import KikiAvatar, { KIKI_STATUS_LABELS } from './KikiAvatar';

export interface SidebarProps {
  topics: Topic[];
  focusTopicId: string | null;
  globalKikiStatus: KikiStatus;
  sseConnected: boolean;
  onSelectAllStream: () => void;
  onSelectTopic: (topicId: string) => void;
  onCreateTopic: (title: string) => Promise<void> | void;
  onReassignMessage: (
    messageId: string,
    payload: ReassignTopicPayload
  ) => Promise<void> | void;
}

export const Sidebar: React.FC<SidebarProps> = ({
  topics,
  focusTopicId,
  globalKikiStatus,
  sseConnected,
  onSelectAllStream,
  onSelectTopic,
  onCreateTopic,
  onReassignMessage,
}) => {
  const [dragOverTopicId, setDragOverTopicId] = useState<string | null>(null);
  const [addingTopic, setAddingTopic] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [creating, setCreating] = useState(false);

  const activeTopics = topics.filter((t) => t.status === 'active');

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
      {/* Brand Header */}
      <div className="px-4 py-4 border-b border-[#E8E2D9] flex items-center justify-between">
        <div className="flex items-center gap-3 min-w-0">
          <img
            src="/kiki-avatar.jpg"
            alt="Kiki"
            className="w-10 h-10 rounded-2xl object-cover border border-[#DDD5C9] shadow-sm shrink-0"
          />
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

            return (
              <button
                key={topic.id}
                type="button"
                onClick={() => onSelectTopic(topic.id)}
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
                className={`w-full group flex items-center justify-between gap-2 px-3 py-2 rounded-xl text-left text-xs transition-all border ${
                  isDragTarget
                    ? 'bg-amber-50 border-amber-400 ring-2 ring-amber-300/60 scale-[1.01]'
                    : isSelected
                    ? 'bg-white text-[#262422] font-semibold border-[#E5DED4] shadow-sm'
                    : 'text-[#4E4842] hover:bg-[#ECE6DD] border-transparent'
                }`}
              >
                <div className="flex items-center gap-2 min-w-0 flex-1">
                  <Hash
                    className={`w-3.5 h-3.5 shrink-0 ${
                      isSelected ? 'text-[#5B7A95]' : 'text-[#9A9187]'
                    }`}
                  />
                  <span className="truncate">{topic.title}</span>
                </div>

                <div className="flex items-center gap-1.5 shrink-0">
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
                    <span className="text-[10px] font-mono text-[#9A9187] tabular-nums">
                      {topic.message_count}
                    </span>
                  )}
                </div>
              </button>
            );
          })
        )}
      </div>

      {/* Bottom Pinned Kiki Status Card */}
      <div className="p-3 border-t border-[#E8E2D9]">
        <div className="rounded-2xl bg-[#FAF8F5] border border-[#E6DFD5] p-3 flex items-center gap-3">
          <KikiAvatar status={globalKikiStatus} size="lg" />
          <div className="min-w-0 flex-1">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-[#8C8378]">
              Kiki 状态
            </div>
            <div className="mt-0.5 text-xs font-semibold text-[#262422] leading-snug">
              {KIKI_STATUS_LABELS[globalKikiStatus]}
            </div>
            <div className="mt-1 text-[10px] text-[#8C8378]">
              拖拽气泡可快速改绑话题
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
};

export default Sidebar;
