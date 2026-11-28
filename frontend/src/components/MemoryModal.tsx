import React, { useCallback, useEffect, useState } from 'react';
import {
  AlertCircle,
  Brain,
  Check,
  Loader2,
  Pencil,
  Pin,
  Plus,
  Search,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import {
  consolidateMemoriesNow,
  createMemory,
  deleteMemory,
  fetchMemories,
  searchMemories,
  updateMemory,
} from '../api';
import type { HistorySearchHit, MemoryCategory, MemoryItem } from '../types';

export interface MemoryModalProps {
  open: boolean;
  onClose: () => void;
  onSelectTopic?: (topicId: string) => void;
}

const CATEGORY_META: Record<MemoryCategory, { label: string; badge: string }> = {
  profile: { label: '个人画像', badge: 'bg-sky-50 text-sky-700 border-sky-200' },
  preference: { label: '偏好约束', badge: 'bg-amber-50 text-amber-700 border-amber-200' },
  project: { label: '项目背景', badge: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
  lesson: { label: '经验踩坑', badge: 'bg-violet-50 text-violet-700 border-violet-200' },
};

export const MemoryModal: React.FC<MemoryModalProps> = ({
  open,
  onClose,
  onSelectTopic,
}) => {
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [historyHits, setHistoryHits] = useState<HistorySearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusNotice, setStatusNotice] = useState<string | null>(null);

  const [categoryFilter, setCategoryFilter] = useState<MemoryCategory | 'all'>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [searching, setSearching] = useState(false);

  const [newContent, setNewContent] = useState('');
  const [newCategory, setNewCategory] = useState<MemoryCategory>('preference');
  const [newPinned, setNewPinned] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingContent, setEditingContent] = useState('');
  const [consolidating, setConsolidating] = useState(false);

  const loadMemories = useCallback(async (cat: MemoryCategory | 'all') => {
    setLoading(true);
    setError(null);
    try {
      const list = await fetchMemories(cat === 'all' ? undefined : cat);
      setMemories(list);
      setHistoryHits([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载记忆失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setStatusNotice(null);
    setSearchQuery('');
    setEditingId(null);
    void loadMemories(categoryFilter);
  }, [open, categoryFilter, loadMemories]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, onClose]);

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    const q = searchQuery.trim();
    if (!q) {
      void loadMemories(categoryFilter);
      return;
    }
    setSearching(true);
    setError(null);
    try {
      const res = await searchMemories(q, true);
      setMemories(res.memories);
      setHistoryHits(res.history);
    } catch (err) {
      setError(err instanceof Error ? err.message : '搜索失败');
    } finally {
      setSearching(false);
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    const content = newContent.trim();
    if (!content || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await createMemory({
        content,
        category: newCategory,
        pinned: newPinned,
      });
      setMemories((prev) => [created, ...prev.filter((m) => m.id !== created.id)]);
      setNewContent('');
      setNewPinned(false);
      setStatusNotice('已保存到长期记忆并同步至 MEMORY.md');
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败');
    } finally {
      setSubmitting(false);
    }
  };

  const handleTogglePin = async (mem: MemoryItem) => {
    try {
      const updated = await updateMemory(mem.id, { pinned: !mem.pinned });
      setMemories((prev) =>
        prev
          .map((m) => (m.id === updated.id ? updated : m))
          .sort((a, b) => Number(b.pinned) - Number(a.pinned))
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新失败');
    }
  };

  const handleSaveEdit = async (memoryId: string) => {
    const trimmed = editingContent.trim();
    if (!trimmed) return;
    try {
      const updated = await updateMemory(memoryId, { content: trimmed });
      setMemories((prev) => prev.map((m) => (m.id === updated.id ? updated : m)));
      setEditingId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : '修改失败');
    }
  };

  const handleDelete = async (memoryId: string) => {
    try {
      await deleteMemory(memoryId);
      setMemories((prev) => prev.filter((m) => m.id !== memoryId));
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败');
    }
  };

  const handleConsolidate = async () => {
    if (consolidating) return;
    setConsolidating(true);
    setError(null);
    setStatusNotice(null);
    try {
      const res = await consolidateMemoriesNow();
      if (!res.ran) {
        setStatusNotice(
          res.reason === 'nothing_to_consolidate'
            ? '当前暂无新的对话需要整理。'
            : `暂未执行整理（${res.reason || 'skipped'}）。`
        );
      } else {
        const added = res.memories_added?.length ?? 0;
        const updated = res.memories_updated?.length ?? 0;
        const summaries = res.updated_summaries?.length ?? 0;
        const migrated = res.migrated_count ?? 0;
        setStatusNotice(
          `整理完成：新增 ${added} 条记忆、更新 ${updated} 条、刷新 ${summaries} 个话题摘要、归类 ${migrated} 条消息。`
        );
        await loadMemories(categoryFilter);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '触发整理失败');
    } finally {
      setConsolidating(false);
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 backdrop-blur-[2px] p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl max-h-[85vh] bg-[#FAF8F5] border border-[#E2D9CC] rounded-2xl shadow-xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-5 py-4 border-b border-[#E8E2D9] flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-xl bg-amber-50 border border-amber-200/80 flex items-center justify-center text-amber-700">
              <Brain className="w-4.5 h-4.5" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-[#262422]">Kiki 长期记忆</h2>
              <p className="text-xs text-[#7D756C]">
                画像与偏好会自动注入对话上下文，并同步至工作区 MEMORY.md
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void handleConsolidate()}
              disabled={consolidating}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-medium bg-white border border-[#DED5C8] text-[#4A443E] hover:bg-[#F3EEE6] disabled:opacity-50 transition-colors"
            >
              {consolidating ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Sparkles className="w-3.5 h-3.5 text-amber-600" />
              )}
              <span>立即整理记忆</span>
            </button>
            <button
              type="button"
              onClick={onClose}
              className="p-1.5 rounded-lg text-[#7D756C] hover:text-[#262422] hover:bg-[#EDE6DC]"
              aria-label="关闭"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Search & Filter */}
        <div className="px-5 py-3 border-b border-[#EDE6DC] space-y-2.5 bg-[#F6F2EC]/60">
          <form onSubmit={handleSearch} className="flex items-center gap-2">
            <div className="relative flex-1">
              <Search className="w-3.5 h-3.5 text-[#8C8378] absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="搜索长期记忆或跨话题历史消息..."
                className="w-full pl-8 pr-3 py-1.5 text-xs bg-white border border-[#DED5C8] rounded-xl text-[#262422] placeholder-[#9B9186] focus:outline-none focus:border-[#5B7A95]"
              />
            </div>
            <button
              type="submit"
              disabled={searching}
              className="px-3 py-1.5 rounded-xl text-xs font-medium bg-[#262422] text-white hover:bg-[#3A3734] disabled:opacity-50"
            >
              {searching ? '搜索中…' : '检索'}
            </button>
            {searchQuery && (
              <button
                type="button"
                onClick={() => {
                  setSearchQuery('');
                  void loadMemories(categoryFilter);
                }}
                className="px-2.5 py-1.5 rounded-xl text-xs text-[#7D756C] hover:bg-[#EAE3D8]"
              >
                清除
              </button>
            )}
          </form>

          <div className="flex flex-wrap items-center gap-1.5">
            {(['all', 'profile', 'preference', 'project', 'lesson'] as const).map((cat) => (
              <button
                key={cat}
                type="button"
                onClick={() => {
                  setSearchQuery('');
                  setCategoryFilter(cat);
                }}
                className={`px-2.5 py-1 rounded-lg text-[11px] font-medium transition-colors ${
                  categoryFilter === cat
                    ? 'bg-[#262422] text-white'
                    : 'bg-white text-[#5A534C] border border-[#E2D9CC] hover:bg-[#EFE9E0]'
                }`}
              >
                {cat === 'all' ? '全部' : CATEGORY_META[cat].label}
              </button>
            ))}
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {error && (
            <div className="flex items-center gap-2 px-3 py-2 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700">
              <AlertCircle className="w-3.5 h-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
          {statusNotice && (
            <div className="px-3 py-2 rounded-xl bg-emerald-50 border border-emerald-200 text-xs text-emerald-700">
              {statusNotice}
            </div>
          )}

          {/* Add Memory Form */}
          <form
            onSubmit={handleCreate}
            className="p-3 rounded-xl bg-white border border-[#E5DED4] space-y-2.5"
          >
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={newContent}
                onChange={(e) => setNewContent(e.target.value)}
                placeholder="手动添加一条长期记忆（例如：前端项目默认使用 pnpm）..."
                className="flex-1 px-3 py-1.5 text-xs bg-[#FAF8F5] border border-[#E2D9CC] rounded-lg text-[#262422] placeholder-[#9B9186] focus:outline-none focus:border-[#5B7A95]"
              />
              <button
                type="submit"
                disabled={!newContent.trim() || submitting}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium bg-[#5B7A95] text-white hover:bg-[#4B6882] disabled:opacity-50"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>记住</span>
              </button>
            </div>
            <div className="flex items-center justify-between text-[11px] text-[#7D756C]">
              <div className="flex items-center gap-1.5">
                <span>分类：</span>
                {(Object.keys(CATEGORY_META) as MemoryCategory[]).map((cat) => (
                  <button
                    key={cat}
                    type="button"
                    onClick={() => setNewCategory(cat)}
                    className={`px-2 py-0.5 rounded-md border transition-colors ${
                      newCategory === cat
                        ? CATEGORY_META[cat].badge
                        : 'border-transparent hover:bg-[#F3EEE6]'
                    }`}
                  >
                    {CATEGORY_META[cat].label}
                  </button>
                ))}
              </div>
              <label className="inline-flex items-center gap-1 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={newPinned}
                  onChange={(e) => setNewPinned(e.target.checked)}
                  className="rounded border-[#CFC5B7]"
                />
                <span>置顶常驻</span>
              </label>
            </div>
          </form>

          {/* Memories List */}
          <div className="space-y-2">
            {loading ? (
              <div className="py-8 flex items-center justify-center text-xs text-[#8C8378] gap-2">
                <Loader2 className="w-4 h-4 animate-spin" />
                <span>加载记忆中…</span>
              </div>
            ) : memories.length === 0 ? (
              <div className="py-8 text-center text-xs text-[#8C8378]">
                {searchQuery ? '未找到匹配的长期记忆。' : '还没有长期记忆，聊天空闲时 Kiki 会自动做梦提炼，或你也可以在上方手动添加。'}
              </div>
            ) : (
              memories.map((mem) => {
                const meta = CATEGORY_META[mem.category] ?? CATEGORY_META.preference;
                const isEditing = editingId === mem.id;
                return (
                  <div
                    key={mem.id}
                    className="group p-3 rounded-xl bg-white border border-[#E6DFD5] hover:border-[#D4C9B9] transition-colors flex items-start justify-between gap-3"
                  >
                    <div className="flex-1 min-w-0 space-y-1">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span
                          className={`px-1.5 py-0.5 rounded-md text-[10px] font-medium border ${meta.badge}`}
                        >
                          {meta.label}
                        </span>
                        {mem.pinned && (
                          <span className="px-1.5 py-0.5 rounded-md text-[10px] font-medium bg-amber-100/70 text-amber-800">
                            置顶
                          </span>
                        )}
                      </div>
                      {isEditing ? (
                        <div className="flex items-center gap-1.5 pt-1">
                          <input
                            type="text"
                            value={editingContent}
                            onChange={(e) => setEditingContent(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') void handleSaveEdit(mem.id);
                              if (e.key === 'Escape') setEditingId(null);
                            }}
                            autoFocus
                            className="flex-1 px-2.5 py-1 text-xs bg-[#FAF8F5] border border-[#5B7A95] rounded-lg focus:outline-none"
                          />
                          <button
                            type="button"
                            onClick={() => void handleSaveEdit(mem.id)}
                            className="p-1 rounded-lg bg-emerald-600 text-white hover:bg-emerald-700"
                            title="保存"
                          >
                            <Check className="w-3.5 h-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => setEditingId(null)}
                            className="p-1 rounded-lg text-[#7D756C] hover:bg-[#EDE6DC]"
                            title="取消"
                          >
                            <X className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      ) : (
                        <p className="text-xs text-[#262422] leading-relaxed break-words">
                          {mem.content}
                        </p>
                      )}
                    </div>
                    {!isEditing && (
                      <div className="flex items-center gap-1 shrink-0 opacity-70 group-hover:opacity-100 transition-opacity">
                        <button
                          type="button"
                          onClick={() => void handleTogglePin(mem)}
                          title={mem.pinned ? '取消置顶' : '置顶常驻'}
                          className={`p-1.5 rounded-lg transition-colors ${
                            mem.pinned
                              ? 'text-amber-600 bg-amber-50'
                              : 'text-[#8C8378] hover:text-[#262422] hover:bg-[#F1ECE4]'
                          }`}
                        >
                          <Pin className="w-3.5 h-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setEditingId(mem.id);
                            setEditingContent(mem.content);
                          }}
                          title="编辑"
                          className="p-1.5 rounded-lg text-[#8C8378] hover:text-[#262422] hover:bg-[#F1ECE4]"
                        >
                          <Pencil className="w-3.5 h-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => void handleDelete(mem.id)}
                          title="删除"
                          className="p-1.5 rounded-lg text-[#8C8378] hover:text-rose-600 hover:bg-rose-50"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>

          {/* Cross-Topic History Search Hits */}
          {historyHits.length > 0 && (
            <div className="pt-2 border-t border-[#E8E2D9] space-y-2">
              <div className="text-[11px] font-semibold text-[#7D756C]">
                跨话题历史消息匹配 ({historyHits.length})
              </div>
              {historyHits.map((hit) => (
                <button
                  key={hit.id}
                  type="button"
                  onClick={() => {
                    if (onSelectTopic) {
                      onSelectTopic(hit.topic_id);
                      onClose();
                    }
                  }}
                  className="w-full text-left p-2.5 rounded-xl bg-[#F5F1EB] hover:bg-[#ECE5DB] border border-[#E5DED4] transition-colors space-y-1"
                >
                  <div className="flex items-center justify-between text-[11px] text-[#7D756C]">
                    <span className="font-medium text-[#5B7A95]">#{hit.topic_title}</span>
                    <span>{hit.role === 'user' ? '你' : 'Kiki'}</span>
                  </div>
                  <p className="text-xs text-[#3A3530] line-clamp-2">{hit.content}</p>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default MemoryModal;
