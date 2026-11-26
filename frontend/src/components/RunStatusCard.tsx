import React, { useEffect, useState } from 'react';
import {
  AlertCircle,
  Ban,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  Download,
  Eye,
  File,
  FileCode2,
  FileImage,
  FileText,
  Loader2,
  Terminal,
  XCircle,
} from 'lucide-react';
import type { HitlOption, ResumeRunPayload, SessionRun } from '../types';
import KikiAvatar from './KikiAvatar';

export interface RunStatusCardProps {
  sessionRun: SessionRun;
  animateAvatar?: boolean;
  onOpenLogDrawer: (runId: string) => void;
  onResumeRun: (runId: string, payload: ResumeRunPayload) => Promise<void> | void;
  onCancelRun: (runId: string) => Promise<void> | void;
}

function parseTimestamp(ts?: string | null): number {
  if (!ts) return Date.now();
  // SQLite CURRENT_TIMESTAMP often returns "YYYY-MM-DD HH:MM:SS" in UTC without 'T'/'Z'
  const normalized = ts.includes('T')
    ? ts
    : `${ts.replace(' ', 'T')}${ts.endsWith('Z') ? '' : 'Z'}`;
  const parsed = Date.parse(normalized);
  return Number.isNaN(parsed) ? Date.now() : parsed;
}

function formatElapsed(ms: number): string {
  const totalSec = Math.max(0, Math.floor(ms / 1000));
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

function isImageArtifact(fileType: string, filePath: string): boolean {
  const t = (fileType || '').toLowerCase();
  const p = (filePath || '').toLowerCase();
  return (
    t.includes('image') ||
    t.includes('png') ||
    t.includes('jpg') ||
    t.includes('jpeg') ||
    t.includes('webp') ||
    t.includes('gif') ||
    t.includes('svg') ||
    /\.(png|jpe?g|webp|gif|svg)$/.test(p)
  );
}

function getArtifactIcon(fileType: string) {
  const t = fileType.toLowerCase();
  if (t.includes('code') || t.includes('py') || t.includes('ts') || t.includes('js') || t.includes('json')) {
    return <FileCode2 className="w-4 h-4 text-[#5B7A95] shrink-0" />;
  }
  if (t.includes('image') || t.includes('png') || t.includes('jpg') || t.includes('svg')) {
    return <FileImage className="w-4 h-4 text-[#8A5628] shrink-0" />;
  }
  if (t.includes('markdown') || t.includes('md') || t.includes('text') || t.includes('pdf')) {
    return <FileText className="w-4 h-4 text-[#3B6244] shrink-0" />;
  }
  return <File className="w-4 h-4 text-[#7A7269] shrink-0" />;
}

const DEFAULT_STEP_TEXT: Record<SessionRun['status'], string> = {
  queued: 'Kiki 正在叼信派单给后台 Agent...',
  running: 'Kiki 正在后台敲键盘专注干活...',
  waiting_input: '等待你的拍板确认...',
  completed: '后台任务已完成',
  failed: '后台任务执行失败',
  cancelled: '任务已取消',
};

const FALLBACK_HITL_OPTIONS: HitlOption[] = [
  { label: '同意继续', value: 'approve', approved: true, style: 'primary' },
  { label: '拒绝并终止', value: 'reject', approved: false, style: 'danger' },
];

export const RunStatusCard: React.FC<RunStatusCardProps> = ({
  sessionRun,
  animateAvatar = true,
  onOpenLogDrawer,
  onResumeRun,
  onCancelRun,
}) => {
  const [expanded, setExpanded] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [hitlComment, setHitlComment] = useState('');
  const [previewArtifactId, setPreviewArtifactId] = useState<string | null>(null);
  const [previewContent, setPreviewContent] = useState<Record<string, string>>({});
  const [previewLoading, setPreviewLoading] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const isActive =
    sessionRun.status === 'queued' ||
    sessionRun.status === 'running' ||
    sessionRun.status === 'waiting_input';

  useEffect(() => {
    if (!isActive) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isActive]);

  const startMs = parseTimestamp(sessionRun.created_at);
  const endMs = isActive ? now : parseTimestamp(sessionRun.updated_at);
  const elapsedLabel = formatElapsed(Math.max(0, endMs - startMs));

  const stepLogs = sessionRun.step_logs ?? [];
  const artifacts = sessionRun.artifacts ?? [];
  const hitl = sessionRun.hitl_request;
  const isWaitingInput = sessionRun.status === 'waiting_input';

  const handleResume = async (opt: HitlOption) => {
    if (actionBusy) return;
    setActionBusy(true);
    try {
      await onResumeRun(sessionRun.id, {
        decision: opt.value,
        approved: opt.approved,
        comment: hitlComment.trim() || undefined,
      });
      setHitlComment('');
    } finally {
      setActionBusy(false);
    }
  };

  const handleToggleArtifactPreview = async (artifactId: string, isImage = false) => {
    if (previewArtifactId === artifactId) {
      setPreviewArtifactId(null);
      return;
    }
    setPreviewArtifactId(artifactId);
    if (isImage) return;
    if (previewContent[artifactId] !== undefined) return;
    setPreviewLoading(true);
    try {
      const res = await fetch(
        `/api/runs/${encodeURIComponent(sessionRun.id)}/artifacts/${encodeURIComponent(artifactId)}`
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      setPreviewContent((prev) => ({
        ...prev,
        [artifactId]: text.length > 8000 ? `${text.slice(0, 8000)}\n... (已截断，请点击下载完整文件)` : text,
      }));
    } catch {
      setPreviewContent((prev) => ({
        ...prev,
        [artifactId]: '无法预览该产物内容，请尝试下载查看。',
      }));
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleCancel = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (actionBusy) return;
    setActionBusy(true);
    try {
      await onCancelRun(sessionRun.id);
    } finally {
      setActionBusy(false);
    }
  };

  const renderStatusIcon = () => {
    switch (sessionRun.status) {
      case 'queued':
        return (
          <KikiAvatar
            status="running"
            pose="delivering"
            animated={animateAvatar}
            size="sm"
          />
        );
      case 'running':
        return (
          <KikiAvatar
            status="running"
            pose="working"
            animated={animateAvatar}
            size="sm"
          />
        );
      case 'waiting_input':
        // HITL card right below shows the animated paw-waving Kiki, so keep L1 bar a crisp badge
        return (
          <div className="w-8 h-8 rounded-full bg-amber-100 border border-amber-300 flex items-center justify-center shrink-0">
            <AlertCircle className="w-4 h-4 text-amber-700" />
          </div>
        );
      case 'completed':
        return (
          <div className="w-8 h-8 rounded-full bg-emerald-50 border border-emerald-200 flex items-center justify-center shrink-0">
            <CheckCircle2 className="w-4 h-4 text-emerald-600" />
          </div>
        );
      case 'failed':
        return (
          <div className="w-8 h-8 rounded-full bg-rose-50 border border-rose-200 flex items-center justify-center shrink-0">
            <XCircle className="w-4 h-4 text-rose-600" />
          </div>
        );
      case 'cancelled':
        return (
          <div className="w-8 h-8 rounded-full bg-stone-100 border border-stone-200 flex items-center justify-center shrink-0">
            <Ban className="w-4 h-4 text-stone-500" />
          </div>
        );
    }
  };

  const cardBorder = isWaitingInput
    ? 'border-amber-400 bg-amber-50/40'
    : sessionRun.status === 'failed'
    ? 'border-rose-200 bg-white'
    : sessionRun.status === 'completed'
    ? 'border-[#DFDDD7] bg-white'
    : 'border-[#D9E2EC] bg-white';

  return (
    <div className="mt-2.5 space-y-2.5">
      {/* Main Three-Layer Card */}
      <div
        className={`rounded-2xl border shadow-sm shadow-stone-900/[0.03] overflow-hidden transition-colors ${cardBorder}`}
      >
        {/* L1 Default Bar (~48px height) */}
        <div
          onClick={() => setExpanded((prev) => !prev)}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              setExpanded((prev) => !prev);
            }
          }}
          className="min-h-[48px] px-3 py-1.5 flex items-center gap-2.5 cursor-pointer hover:bg-[#FAF8F5]/80 transition-colors select-none"
        >
          {renderStatusIcon()}

          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-md bg-[#EFEAE2] text-[#575049] shrink-0">
                {sessionRun.agent_type}
              </span>
              <span className="text-xs font-medium text-[#262422] break-words">
                {sessionRun.current_step || DEFAULT_STEP_TEXT[sessionRun.status]}
              </span>
              {stepLogs.length > 0 && (
                <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-[#F3EFE9] text-[#7A7269] shrink-0">
                  {stepLogs.length} 步
                </span>
              )}
              {artifacts.length > 0 && (
                <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-[#EAF0F5] text-[#456580] shrink-0">
                  {artifacts.length} 产物
                </span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-1.5 shrink-0">
            <span className="inline-flex items-center gap-1 text-[11px] font-mono text-[#7D756C] tabular-nums">
              <Clock className="w-3 h-3 opacity-70" />
              {elapsedLabel}
            </span>

            {/* L3 Terminal Drawer Trigger */}
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onOpenLogDrawer(sessionRun.id);
              }}
              title="打开 L3 极客终端日志 (NDJSON / Stdout)"
              className="inline-flex items-center gap-0.5 px-1.5 py-1 rounded-lg text-[11px] font-mono font-medium text-[#5A534C] hover:text-[#262422] hover:bg-[#EFEAE2] border border-transparent hover:border-[#E2DBD0] transition-colors"
            >
              <Terminal className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">日志</span>
            </button>

            {isActive && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={handleCancel}
                className="px-2 py-1 rounded-lg text-[11px] font-medium text-rose-700 bg-rose-50 hover:bg-rose-100 border border-rose-200/80 transition-colors disabled:opacity-50"
              >
                取消
              </button>
            )}

            <span className="p-1 text-[#8C8378]">
              {expanded ? (
                <ChevronUp className="w-4 h-4" />
              ) : (
                <ChevronDown className="w-4 h-4" />
              )}
            </span>
          </div>
        </div>

        {/* L2 Expanded Layer: Step Timeline + Artifacts */}
        {expanded && (
          <div className="border-t border-[#EFEAE2] bg-[#FCFBF9] px-3.5 py-3 space-y-3 animate-kiki-fade-up">
            {sessionRun.error_message && (
              <div className="rounded-xl bg-rose-50 border border-rose-200 px-3 py-2 text-xs text-rose-800 flex items-start gap-2">
                <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-rose-600" />
                <span className="break-words">{sessionRun.error_message}</span>
              </div>
            )}

            {/* Step Logs Timeline */}
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-wider text-[#8A8177] mb-2">
                执行步骤时间线 ({stepLogs.length})
              </div>
              {stepLogs.length === 0 ? (
                <p className="text-xs text-[#9A9187] italic">暂无已记录的执行步骤</p>
              ) : (
                <ol className="relative border-l border-[#E5DED4] ml-2 space-y-2.5 pl-4">
                  {stepLogs.map((step) => (
                    <li key={step.id} className="relative text-xs">
                      <span className="absolute -left-[22px] top-0.5 bg-[#FCFBF9] p-0.5 rounded-full">
                        {step.status === 'success' ? (
                          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" />
                        ) : step.status === 'failed' ? (
                          <XCircle className="w-3.5 h-3.5 text-rose-500" />
                        ) : (
                          <Loader2 className="w-3.5 h-3.5 text-[#5B7A95] animate-spin" />
                        )}
                      </span>
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-[#EFEAE2] text-[#575049]">
                          #{step.step_number} {step.action_type}
                        </span>
                        <span className="text-[#2E2B28] break-words">
                          {step.detail}
                        </span>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </div>

            {/* Artifacts List */}
            {artifacts.length > 0 && (
              <div className="pt-1 border-t border-[#EFEAE2]">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-[#8A8177] mb-2">
                  生成产物 ({artifacts.length})
                </div>
                <div className="space-y-1.5">
                  {artifacts.map((art) => {
                    const artUrl = `/api/runs/${encodeURIComponent(sessionRun.id)}/artifacts/${encodeURIComponent(art.id)}`;
                    const isImage = isImageArtifact(art.file_type, art.file_path);
                    const isPreviewOpen = previewArtifactId === art.id;
                    return (
                      <div
                        key={art.id}
                        className="rounded-xl bg-white border border-[#E8E2D9] overflow-hidden"
                      >
                        <div className="flex items-start gap-2.5 px-2.5 py-2">
                          {getArtifactIcon(art.file_type)}
                          <div className="min-w-0 flex-1">
                            <div className="text-xs font-mono font-medium text-[#262422] truncate">
                              {art.file_path}
                            </div>
                            {art.description && (
                              <div className="text-[11px] text-[#7A7269] mt-0.5">
                                {art.description}
                              </div>
                            )}
                          </div>
                          <div className="flex items-center gap-1 shrink-0">
                            <button
                              type="button"
                              onClick={() => void handleToggleArtifactPreview(art.id, isImage)}
                              title="在线预览产物"
                              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] font-medium bg-[#FAF8F5] hover:bg-[#EFEAE2] border border-[#E8E2D9] text-[#4E4842] transition-colors"
                            >
                              <Eye className="w-3 h-3 text-[#5B7A95]" />
                              <span>{isPreviewOpen ? '收起' : '预览'}</span>
                            </button>
                            <a
                              href={`${artUrl}?download=true`}
                              download
                              title="下载产物文件"
                              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] font-medium bg-[#FAF8F5] hover:bg-[#EFEAE2] border border-[#E8E2D9] text-[#4E4842] transition-colors"
                            >
                              <Download className="w-3 h-3 text-[#5B7A95]" />
                              <span>下载</span>
                            </a>
                          </div>
                        </div>
                        {isPreviewOpen && (
                          <div className="border-t border-[#EFEAE2] bg-[#FAF8F5] px-3 py-2 max-h-80 overflow-auto">
                            {isImage ? (
                              <a href={artUrl} target="_blank" rel="noopener noreferrer" className="block">
                                <img
                                  src={artUrl}
                                  alt={art.description || art.file_path}
                                  className="max-h-72 max-w-full rounded-lg border border-[#E8E2D9] object-contain mx-auto bg-white"
                                />
                              </a>
                            ) : previewLoading && previewContent[art.id] === undefined ? (
                              <span className="text-[11px] text-[#8C8378]">正在加载产物内容...</span>
                            ) : (
                              <pre className="text-[11px] font-mono text-[#2E2B28] whitespace-pre-wrap break-words leading-relaxed">
                                {previewContent[art.id] ?? ''}
                              </pre>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* HITL Approval Card */}
      {isWaitingInput && (
        <div className="rounded-2xl border-2 border-amber-400 bg-amber-50/80 p-3.5 shadow-sm shadow-amber-900/5 animate-kiki-fade-up">
          <div className="flex items-start gap-3">
            <KikiAvatar
              status="waiting_input"
              animated={animateAvatar}
              size="md"
              className="mt-0.5"
            />
            <div className="flex-1 min-w-0">
              <div className="inline-flex items-center gap-1.5 text-xs font-semibold text-amber-900">
                <span className="w-2 h-2 rounded-full bg-amber-500 animate-pulse" />
                Kiki 需要你拍板
              </div>
              <p className="mt-1 text-sm text-[#2E271E] leading-relaxed">
                {hitl?.prompt || sessionRun.current_step || '后台任务需要你的确认才能继续执行。'}
              </p>

              <input
                type="text"
                value={hitlComment}
                onChange={(e) => setHitlComment(e.target.value)}
                disabled={actionBusy}
                placeholder="可选：补充说明或限定条件（点击下方按钮一并提交）"
                className="mt-2.5 w-full px-3 py-1.5 rounded-xl text-xs bg-white/90 border border-amber-300/90 text-[#2E271E] placeholder:text-amber-800/50 focus:outline-none focus:border-amber-500"
              />

              <div className="mt-2.5 flex flex-wrap items-center gap-2">
                {(hitl?.options && hitl.options.length > 0
                  ? hitl.options
                  : FALLBACK_HITL_OPTIONS
                ).map((opt) => {
                  const isPrimary = opt.style === 'primary' || (opt.approved && opt.style !== 'danger');
                  const isDanger = opt.style === 'danger' || !opt.approved;
                  const btnClass = isPrimary
                    ? 'bg-[#D97706] hover:bg-[#B45309] text-white border-transparent shadow-sm'
                    : isDanger
                    ? 'bg-white hover:bg-rose-50 text-rose-700 border-rose-300'
                    : 'bg-white hover:bg-amber-100/60 text-amber-900 border-amber-300';

                  return (
                    <button
                      key={opt.value}
                      type="button"
                      disabled={actionBusy}
                      onClick={() => handleResume(opt)}
                      className={`px-3.5 py-1.5 rounded-xl text-xs font-semibold border transition-all active:scale-[0.98] disabled:opacity-50 ${btnClass}`}
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default RunStatusCard;
