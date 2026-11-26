import React, { useCallback, useEffect, useState } from 'react';
import {
  AlertCircle,
  ArrowUp,
  Check,
  ChevronRight,
  Copy,
  Folder,
  FolderOpen,
  FolderPlus,
  FolderSearch,
  Loader2,
  Star,
  Trash2,
  X,
} from 'lucide-react';
import {
  addWorkspaceMount,
  browseDirectories,
  deleteWorkspaceMount,
  fetchGlobalWorkspace,
  fetchTopicWorkspace,
  updateWorkspaceMount,
} from '../api';
import type {
  DirectoryBrowseResponse,
  GlobalWorkspace,
  Topic,
  TopicWorkspace,
  WorkspaceMount,
} from '../types';

export interface WorkspaceModalProps {
  open: boolean;
  focusTopic: Topic | null;
  onClose: () => void;
  onUpdated?: () => void;
}

function shortBasename(p: string): string {
  const cleaned = p.replace(/\/+$/, '');
  const parts = cleaned.split('/');
  return parts[parts.length - 1] || p;
}

export const WorkspaceModal: React.FC<WorkspaceModalProps> = ({
  open,
  focusTopic,
  onClose,
  onUpdated,
}) => {
  const [activeTab, setActiveTab] = useState<'topic' | 'global'>(
    focusTopic ? 'topic' : 'global'
  );
  const [globalWs, setGlobalWs] = useState<GlobalWorkspace | null>(null);
  const [topicWs, setTopicWs] = useState<TopicWorkspace | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedPath, setCopiedPath] = useState<string | null>(null);

  const [newPath, setNewPath] = useState('');
  const [newLabel, setNewLabel] = useState('');
  const [newIsPrimary, setNewIsPrimary] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const [browserOpen, setBrowserOpen] = useState(false);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [browserError, setBrowserError] = useState<string | null>(null);
  const [showHiddenDirs, setShowHiddenDirs] = useState(false);
  const [browseData, setBrowseData] = useState<DirectoryBrowseResponse | null>(null);

  useEffect(() => {
    if (open) {
      setActiveTab(focusTopic ? 'topic' : 'global');
      setError(null);
      setNewPath('');
      setNewLabel('');
      setNewIsPrimary(Boolean(focusTopic));
      setBrowserOpen(false);
      setBrowserError(null);
    }
  }, [open, focusTopic]);

  const loadBrowseDir = useCallback(
    async (targetPath?: string, hiddenOverride?: boolean) => {
      setBrowserLoading(true);
      setBrowserError(null);
      try {
        const res = await browseDirectories(
          targetPath,
          hiddenOverride ?? showHiddenDirs
        );
        setBrowseData(res);
      } catch (err) {
        setBrowserError(err instanceof Error ? err.message : '无法读取该目录');
      } finally {
        setBrowserLoading(false);
      }
    },
    [showHiddenDirs]
  );

  const handleToggleBrowser = () => {
    if (browserOpen) {
      setBrowserOpen(false);
      return;
    }
    setBrowserOpen(true);
    void loadBrowseDir(newPath.trim() || undefined);
  };

  const handlePickDirectory = (pickedPath: string) => {
    setNewPath(pickedPath);
    setBrowserOpen(false);
  };

  const loadData = useCallback(async () => {
    if (!open) return;
    setLoading(true);
    setError(null);
    try {
      const g = await fetchGlobalWorkspace();
      setGlobalWs(g);
      if (focusTopic) {
        const t = await fetchTopicWorkspace(focusTopic.id);
        setTopicWs(t);
      } else {
        setTopicWs(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载工作空间信息失败');
    } finally {
      setLoading(false);
    }
  }, [open, focusTopic]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const handleCopy = async (path: string) => {
    try {
      await navigator.clipboard.writeText(path);
      setCopiedPath(path);
      window.setTimeout(() => setCopiedPath((prev) => (prev === path ? null : prev)), 1600);
    } catch {
      // ignore clipboard failures
    }
  };

  const handleAddMount = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedPath = newPath.trim();
    if (!trimmedPath || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const scope = activeTab === 'topic' && focusTopic ? 'topic' : 'global';
      await addWorkspaceMount({
        scope,
        topic_id: scope === 'topic' ? focusTopic?.id : null,
        path: trimmedPath,
        label: newLabel.trim() || undefined,
        is_primary: newIsPrimary,
      });
      setNewPath('');
      setNewLabel('');
      setNewIsPrimary(false);
      await loadData();
      onUpdated?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : '添加目录失败');
    } finally {
      setSubmitting(false);
    }
  };

  const handleTogglePrimary = async (mount: WorkspaceMount) => {
    setError(null);
    try {
      await updateWorkspaceMount(mount.id, { is_primary: !mount.is_primary });
      await loadData();
      onUpdated?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新主目录状态失败');
    }
  };

  const handleDeleteMount = async (mountId: string) => {
    setError(null);
    try {
      await deleteWorkspaceMount(mountId);
      await loadData();
      onUpdated?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : '移除目录失败');
    }
  };

  const renderPathRow = (title: string, path: string, hint?: string) => (
    <div
      key={`${title}-${path}`}
      className="flex items-center justify-between gap-2 px-3 py-2 rounded-xl bg-[#FAF8F5] border border-[#E8E2D9]"
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="text-xs font-medium text-[#262422]">{title}</span>
          {hint && <span className="text-[10px] text-[#8C8378]">{hint}</span>}
        </div>
        <div className="text-[11px] font-mono text-[#5A534C] truncate mt-0.5" title={path}>
          {path}
        </div>
      </div>
      <button
        type="button"
        onClick={() => void handleCopy(path)}
        title="复制路径"
        className="p-1.5 rounded-lg text-[#7D756C] hover:text-[#262422] hover:bg-[#ECE6DD] transition-colors shrink-0"
      >
        {copiedPath === path ? (
          <Check className="w-3.5 h-3.5 text-emerald-600" />
        ) : (
          <Copy className="w-3.5 h-3.5" />
        )}
      </button>
    </div>
  );

  const renderMountList = (mounts: WorkspaceMount[], emptyText: string) => {
    if (mounts.length === 0) {
      return (
        <div className="px-3 py-4 rounded-xl border border-dashed border-[#DFD7CB] text-center text-xs text-[#8C8378]">
          {emptyText}
        </div>
      );
    }
    return (
      <div className="space-y-2">
        {mounts.map((m) => (
          <div
            key={m.id}
            className="flex items-center justify-between gap-2 px-3 py-2 rounded-xl bg-white border border-[#E5DED4] shadow-2xs"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5 flex-wrap">
                <Folder className="w-3.5 h-3.5 text-[#5B7A95] shrink-0" />
                <span className="text-xs font-medium text-[#262422] truncate">
                  {m.label || shortBasename(m.path)}
                </span>
                {m.is_primary && (
                  <span className="px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-[#EAF0F5] text-[#3B5972] border border-[#CFE0EC]">
                    主工作目录 (cwd)
                  </span>
                )}
                {!m.exists && (
                  <span className="px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-rose-50 text-rose-700 border border-rose-200">
                    路径不存在
                  </span>
                )}
              </div>
              <div className="text-[11px] font-mono text-[#6E665E] truncate mt-0.5" title={m.path}>
                {m.path}
              </div>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <button
                type="button"
                onClick={() => void handleTogglePrimary(m)}
                title={m.is_primary ? '取消主工作目录标记' : '设为 Agent 主工作目录 (cwd)'}
                className={`p-1.5 rounded-lg transition-colors ${
                  m.is_primary
                    ? 'text-amber-600 bg-amber-50 hover:bg-amber-100'
                    : 'text-[#8C8378] hover:text-amber-600 hover:bg-[#F3EFE9]'
                }`}
              >
                <Star className={`w-3.5 h-3.5 ${m.is_primary ? 'fill-amber-500' : ''}`} />
              </button>
              <button
                type="button"
                onClick={() => void handleCopy(m.path)}
                title="复制路径"
                className="p-1.5 rounded-lg text-[#7D756C] hover:text-[#262422] hover:bg-[#F3EFE9] transition-colors"
              >
                {copiedPath === m.path ? (
                  <Check className="w-3.5 h-3.5 text-emerald-600" />
                ) : (
                  <Copy className="w-3.5 h-3.5" />
                )}
              </button>
              <button
                type="button"
                onClick={() => void handleDeleteMount(m.id)}
                title="移除此目录"
                className="p-1.5 rounded-lg text-[#7D756C] hover:text-rose-700 hover:bg-rose-50 transition-colors"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        ))}
      </div>
    );
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        onClick={onClose}
        className="fixed inset-0 bg-black/30 backdrop-blur-[1px]"
      />
      <div className="relative z-10 w-full max-w-xl max-h-[85vh] overflow-hidden rounded-2xl bg-[#FAF8F5] border border-[#E2D9CC] shadow-2xl flex flex-col">
        {/* Header */}
        <div className="px-5 py-3.5 border-b border-[#E8E2D9] flex items-center justify-between gap-3 bg-white/80">
          <div className="flex items-center gap-2 min-w-0">
            <FolderOpen className="w-4 h-4 text-[#5B7A95] shrink-0" />
            <h2 className="text-sm font-semibold text-[#262422] truncate">
              Kiki 工作空间与协作目录
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭工作空间面板"
            className="p-1 rounded-lg text-[#7D756C] hover:text-[#262422] hover:bg-[#EFEAE2]"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Scope Tabs */}
        <div className="px-5 pt-3 flex items-center gap-2 border-b border-[#EAE4DB] bg-white/40">
          {focusTopic && (
            <button
              type="button"
              onClick={() => {
                setActiveTab('topic');
                setNewIsPrimary(true);
              }}
              className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors ${
                activeTab === 'topic'
                  ? 'border-[#5B7A95] text-[#262422]'
                  : 'border-transparent text-[#7D756C] hover:text-[#262422]'
              }`}
            >
              当前话题 #{focusTopic.title}
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              setActiveTab('global');
              setNewIsPrimary(false);
            }}
            className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors ${
              activeTab === 'global'
                ? 'border-[#5B7A95] text-[#262422]'
                : 'border-transparent text-[#7D756C] hover:text-[#262422]'
            }`}
          >
            主信息流 / 全局工作空间
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {error && (
            <div className="px-3 py-2 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800 flex items-center gap-2">
              <AlertCircle className="w-4 h-4 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {loading ? (
            <div className="py-10 flex items-center justify-center text-xs text-[#8C8378] gap-2">
              <Loader2 className="w-4 h-4 animate-spin" />
              正在加载目录信息...
            </div>
          ) : activeTab === 'topic' && focusTopic && topicWs ? (
            <>
              <div className="space-y-2">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-[#8C8378]">
                  当前话题生效目录（调用其他 Agent 时自动透传）
                </div>
                {renderPathRow(
                  'Agent 主工作目录 (cwd)',
                  topicWs.primary_dir,
                  topicWs.primary_dir === topicWs.topic_dir
                    ? '默认使用话题子目录'
                    : '已指向话题自定义目录'
                )}
                {renderPathRow('话题专属子目录', topicWs.topic_dir, '自动创建')}
                {renderPathRow('话题产物目录 (artifacts)', topicWs.topic_artifacts_dir)}
              </div>

              <div className="space-y-2">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-[#8C8378]">
                  为本话题指定的外部目录 ({topicWs.topic_mounts.length})
                </div>
                {renderMountList(
                  topicWs.topic_mounts,
                  '尚未指定外部目录，后台 Agent 默认在话题专属子目录内工作。'
                )}
              </div>

              {topicWs.global_mounts.length > 0 && (
                <div className="space-y-1.5">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-[#8C8378]">
                    继承自主信息流的共享目录 ({topicWs.global_mounts.length})
                  </div>
                  <div className="space-y-1">
                    {topicWs.global_mounts.map((gm) => (
                      <div
                        key={gm.id}
                        className="px-3 py-1.5 rounded-lg bg-[#F3EFE9] text-[11px] font-mono text-[#5A534C] truncate"
                        title={gm.path}
                      >
                        {gm.label ? `${gm.label}: ` : ''}
                        {gm.path}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          ) : globalWs ? (
            <>
              <div className="space-y-2">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-[#8C8378]">
                  Kiki 专属工作空间
                </div>
                {renderPathRow('工作空间根目录', globalWs.workspace_root)}
                {renderPathRow('全局规则目录 (rules)', globalWs.rules_dir, '供 Kiki 与外部 Agent 共享')}
                {renderPathRow('共享资源目录 (shared)', globalWs.shared_dir, '跨话题公共文件')}
                {renderPathRow('话题目录根 (topics)', globalWs.topics_dir, '每个话题自动分配子目录')}
              </div>

              <div className="space-y-2">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-[#8C8378]">
                  主信息流自定义目录 ({globalWs.global_mounts.length})
                </div>
                {renderMountList(
                  globalWs.global_mounts,
                  '尚未为主信息流添加额外目录。添加后，所有话题调用 Agent 时都会将其作为协作目录透传。'
                )}
              </div>
            </>
          ) : null}

          {/* Add Mount Form */}
          <form
            onSubmit={handleAddMount}
            className="p-3.5 rounded-xl bg-white border border-[#E5DED4] space-y-2.5"
          >
            <div className="flex items-center gap-1.5 text-xs font-medium text-[#262422]">
              <FolderPlus className="w-3.5 h-3.5 text-[#5B7A95]" />
              <span>
                {activeTab === 'topic' && focusTopic
                  ? `为「${focusTopic.title}」添加指定目录`
                  : '为主信息流添加指定目录'}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={newPath}
                onChange={(e) => setNewPath(e.target.value)}
                placeholder="输入本地文件夹路径，例如 ~/projects/my-app"
                className="flex-1 min-w-0 px-3 py-2 text-xs font-mono rounded-lg bg-[#FAF8F5] border border-[#E2D9CC] focus:outline-none focus:border-[#5B7A95]"
              />
              <button
                type="button"
                onClick={handleToggleBrowser}
                className={`inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium border transition-colors shrink-0 ${
                  browserOpen
                    ? 'bg-[#EAF0F5] text-[#3B5972] border-[#CFE0EC]'
                    : 'bg-[#FAF8F5] hover:bg-[#EFEAE2] text-[#4E4842] border-[#E2D9CC]'
                }`}
              >
                <FolderSearch className="w-3.5 h-3.5 text-[#5B7A95]" />
                <span>{browserOpen ? '收起浏览' : '浏览...'}</span>
              </button>
            </div>

            {browserOpen && (
              <div className="rounded-xl border border-[#DFD7CB] bg-[#FAF8F5] overflow-hidden">
                {/* Browser Toolbar */}
                <div className="px-2.5 py-2 bg-[#F3EFE9] border-b border-[#E5DED4] flex items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5 min-w-0 flex-1">
                    <button
                      type="button"
                      disabled={browserLoading || !browseData?.parent_path}
                      onClick={() => {
                        if (browseData?.parent_path) {
                          void loadBrowseDir(browseData.parent_path);
                        }
                      }}
                      title="返回上一级目录"
                      className="p-1 rounded-md bg-white border border-[#E2D9CC] text-[#4E4842] hover:bg-[#FAF8F5] disabled:opacity-40 shrink-0"
                    >
                      <ArrowUp className="w-3.5 h-3.5" />
                    </button>
                    <span
                      className="text-[11px] font-mono text-[#3A3530] truncate"
                      title={browseData?.current_path || ''}
                    >
                      {browseData?.current_path || '正在加载...'}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <label className="inline-flex items-center gap-1 text-[11px] text-[#6E665E] cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={showHiddenDirs}
                        onChange={(e) => {
                          const next = e.target.checked;
                          setShowHiddenDirs(next);
                          void loadBrowseDir(browseData?.current_path, next);
                        }}
                        className="rounded border-[#C8BFB2] text-[#5B7A95] focus:ring-[#5B7A95]"
                      />
                      <span>隐藏目录</span>
                    </label>
                    <button
                      type="button"
                      disabled={!browseData?.current_path}
                      onClick={() => {
                        if (browseData?.current_path) {
                          handlePickDirectory(browseData.current_path);
                        }
                      }}
                      className="px-2.5 py-1 rounded-md text-[11px] font-medium bg-[#5B7A95] hover:bg-[#4B6882] text-white transition-colors disabled:opacity-40"
                    >
                      选择此目录
                    </button>
                  </div>
                </div>

                {/* Quick Shortcuts */}
                {browseData && browseData.shortcuts.length > 0 && (
                  <div className="px-2.5 py-1.5 border-b border-[#EAE3D8] flex items-center gap-1.5 overflow-x-auto">
                    {browseData.shortcuts.map((sc) => {
                      const active = browseData.current_path === sc.path;
                      return (
                        <button
                          key={sc.path}
                          type="button"
                          onClick={() => void loadBrowseDir(sc.path)}
                          className={`px-2 py-0.5 rounded-md text-[11px] whitespace-nowrap transition-colors ${
                            active
                              ? 'bg-[#5B7A95] text-white font-medium'
                              : 'bg-white border border-[#E5DED4] text-[#5A534C] hover:bg-[#EFEAE2]'
                          }`}
                        >
                          {sc.name}
                        </button>
                      );
                    })}
                  </div>
                )}

                {browserError && (
                  <div className="px-3 py-2 text-[11px] text-rose-700 bg-rose-50 border-b border-rose-200 flex items-center justify-between gap-2">
                    <span className="truncate">{browserError}</span>
                    <button
                      type="button"
                      onClick={() => void loadBrowseDir(undefined)}
                      className="text-[11px] font-medium underline shrink-0"
                    >
                      回到主目录
                    </button>
                  </div>
                )}

                {/* Directory Entries */}
                <div className="max-h-48 overflow-y-auto divide-y divide-[#EFEAE2]">
                  {browserLoading ? (
                    <div className="py-6 flex items-center justify-center gap-2 text-xs text-[#7D756C]">
                      <Loader2 className="w-3.5 h-3.5 animate-spin text-[#5B7A95]" />
                      <span>正在读取目录...</span>
                    </div>
                  ) : browseData && browseData.entries.length > 0 ? (
                    browseData.entries.map((entry) => (
                      <div
                        key={entry.path}
                        className="group flex items-center justify-between gap-2 px-3 py-1.5 hover:bg-white transition-colors"
                      >
                        <button
                          type="button"
                          onClick={() => void loadBrowseDir(entry.path)}
                          className="flex-1 min-w-0 flex items-center gap-2 text-left text-xs text-[#262422]"
                        >
                          <Folder className="w-3.5 h-3.5 text-[#5B7A95] shrink-0" />
                          <span className="truncate">{entry.name}</span>
                          <ChevronRight className="w-3 h-3 text-[#ABA298] shrink-0 opacity-0 group-hover:opacity-100" />
                        </button>
                        <button
                          type="button"
                          onClick={() => handlePickDirectory(entry.path)}
                          className="opacity-0 group-hover:opacity-100 px-2 py-0.5 rounded text-[11px] font-medium text-[#3B5972] bg-[#EAF0F5] hover:bg-[#DCE7F0] transition-opacity shrink-0"
                        >
                          选择
                        </button>
                      </div>
                    ))
                  ) : (
                    <div className="py-5 text-center text-[11px] text-[#8C8378]">
                      当前目录下没有子文件夹，可直接点击右上角「选择此目录」
                    </div>
                  )}
                </div>
              </div>
            )}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <input
                type="text"
                value={newLabel}
                onChange={(e) => setNewLabel(e.target.value)}
                placeholder="可选备注名（如：后端代码库）"
                className="flex-1 min-w-[160px] px-3 py-1.5 text-xs rounded-lg bg-[#FAF8F5] border border-[#E2D9CC] focus:outline-none focus:border-[#5B7A95]"
              />
              <label className="inline-flex items-center gap-1.5 text-xs text-[#5A534C] cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={newIsPrimary}
                  onChange={(e) => setNewIsPrimary(e.target.checked)}
                  className="rounded border-[#C8BFB2] text-[#5B7A95] focus:ring-[#5B7A95]"
                />
                <span>设为 Agent 主目录 (cwd)</span>
              </label>
              <button
                type="submit"
                disabled={!newPath.trim() || submitting}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-[#5B7A95] hover:bg-[#4B6882] text-white transition-colors disabled:opacity-40"
              >
                {submitting ? '添加中...' : '添加目录'}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
};

export default WorkspaceModal;
