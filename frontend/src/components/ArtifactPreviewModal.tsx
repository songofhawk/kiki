import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  File,
  FileCode2,
  FileImage,
  FileText,
  Loader2,
  RotateCcw,
  RotateCw,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import type { Artifact } from '../types';

export interface ArtifactPreviewItem {
  runId: string;
  artifact: Artifact;
}

export interface ArtifactPreviewModalProps {
  items: ArtifactPreviewItem[];
  activeIndex: number;
  onChangeIndex: (index: number) => void;
  onClose: () => void;
}

const MIN_SCALE = 0.25;
const MAX_SCALE = 5;
const BUTTON_ZOOM_STEP = 0.25;
const MAX_TEXT_PREVIEW_CHARS = 200_000;

export function getArtifactUrl(runId: string, artifactId: string): string {
  return `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`;
}

export function isImageArtifact(fileType: string, filePath: string): boolean {
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
    t.includes('bmp') ||
    /\.(png|jpe?g|webp|gif|svg|bmp|ico)$/.test(p)
  );
}

export function isPdfArtifact(fileType: string, filePath: string): boolean {
  const t = (fileType || '').toLowerCase();
  const p = (filePath || '').toLowerCase();
  return t.includes('pdf') || /\.pdf$/.test(p);
}

function getArtifactBadgeIcon(fileType: string, filePath: string) {
  if (isImageArtifact(fileType, filePath)) {
    return <FileImage className="w-4 h-4 text-amber-300 shrink-0" />;
  }
  const t = (fileType || '').toLowerCase();
  if (
    t.includes('code') ||
    t.includes('py') ||
    t.includes('ts') ||
    t.includes('js') ||
    t.includes('json')
  ) {
    return <FileCode2 className="w-4 h-4 text-sky-300 shrink-0" />;
  }
  if (t.includes('markdown') || t.includes('md') || t.includes('text') || t.includes('pdf')) {
    return <FileText className="w-4 h-4 text-emerald-300 shrink-0" />;
  }
  return <File className="w-4 h-4 text-stone-300 shrink-0" />;
}

function clampScale(val: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, Math.round(val * 100) / 100));
}

function isKnownTextArtifact(fileType: string, filePath: string): boolean {
  const t = (fileType || '').toLowerCase();
  const p = (filePath || '').toLowerCase();
  return (
    t.includes('text') ||
    t.includes('markdown') ||
    t.includes('code') ||
    t.includes('json') ||
    t.includes('yaml') ||
    t.includes('xml') ||
    t.includes('csv') ||
    /\.(md|markdown|txt|log|json|jsonl|ya?ml|toml|csv|tsv|py|tsx?|jsx?|mjs|cjs|css|scss|less|html?|xml|svg|sql|sh|bash|zsh|ini|cfg|conf|rs|go|java|c|cpp|h|hpp)$/.test(
      p
    )
  );
}

function isBinaryContentType(
  contentType: string,
  fileType = '',
  filePath = ''
): boolean {
  const ct = contentType.toLowerCase();
  if (!ct) return false;
  if (
    ct.startsWith('text/') ||
    ct.includes('json') ||
    ct.includes('xml') ||
    ct.includes('javascript') ||
    ct.includes('typescript') ||
    ct.includes('yaml') ||
    ct.includes('toml') ||
    ct.includes('csv') ||
    ct.includes('sql') ||
    ct.includes('sh') ||
    ct.includes('markdown')
  ) {
    return false;
  }
  if (ct.startsWith('application/octet-stream')) {
    return !isKnownTextArtifact(fileType, filePath);
  }
  return (
    ct.startsWith('application/zip') ||
    ct.startsWith('application/gzip') ||
    ct.startsWith('application/x-tar') ||
    ct.startsWith('audio/') ||
    ct.startsWith('video/') ||
    ct.startsWith('font/')
  );
}

export const ArtifactPreviewModal: React.FC<ArtifactPreviewModalProps> = ({
  items,
  activeIndex,
  onChangeIndex,
  onClose,
}) => {
  const current =
    activeIndex >= 0 && activeIndex < items.length ? items[activeIndex] : null;

  const [scale, setScale] = useState(1);
  const [rotation, setRotation] = useState(0);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);

  const [imageLoading, setImageLoading] = useState(true);
  const [imageError, setImageError] = useState(false);

  const [textState, setTextState] = useState<{
    loading: boolean;
    content: string | null;
    binary: boolean;
    error: string | null;
  }>({ loading: false, content: null, binary: false, error: null });
  const [copied, setCopied] = useState(false);

  const viewportRef = useRef<HTMLDivElement | null>(null);
  const dragStartRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    origX: number;
    origY: number;
    moved: boolean;
  } | null>(null);
  const suppressBackdropClickRef = useRef(false);

  const currentRunId = current?.runId ?? '';
  const currentArtifact = current?.artifact ?? null;
  const currentUrl = currentArtifact ? getArtifactUrl(currentRunId, currentArtifact.id) : '';
  const isImage = currentArtifact
    ? isImageArtifact(currentArtifact.file_type, currentArtifact.file_path)
    : false;
  const isPdf = currentArtifact
    ? !isImage && isPdfArtifact(currentArtifact.file_type, currentArtifact.file_path)
    : false;
  const isTextLike = Boolean(currentArtifact && !isImage && !isPdf);

  const handleResetView = useCallback(() => {
    setScale(1);
    setRotation(0);
    setOffset({ x: 0, y: 0 });
  }, []);

  const handleZoomIn = useCallback(() => {
    setScale((prev) => clampScale(prev + BUTTON_ZOOM_STEP));
  }, []);

  const handleZoomOut = useCallback(() => {
    setScale((prev) => {
      const next = clampScale(prev - BUTTON_ZOOM_STEP);
      if (next <= 1) {
        setOffset({ x: 0, y: 0 });
      }
      return next;
    });
  }, []);

  const handleRotate = useCallback(() => {
    setRotation((prev) => (prev + 90) % 360);
  }, []);

  const hasMultiple = items.length > 1;
  const handlePrev = useCallback(() => {
    if (!hasMultiple) return;
    onChangeIndex((activeIndex - 1 + items.length) % items.length);
  }, [hasMultiple, activeIndex, items.length, onChangeIndex]);

  const handleNext = useCallback(() => {
    if (!hasMultiple) return;
    onChangeIndex((activeIndex + 1) % items.length);
  }, [hasMultiple, activeIndex, items.length, onChangeIndex]);

  // Reset view whenever switching artifact
  useEffect(() => {
    setScale(1);
    setRotation(0);
    setOffset({ x: 0, y: 0 });
    setIsDragging(false);
    setImageLoading(true);
    setImageError(false);
    setCopied(false);
    dragStartRef.current = null;
  }, [currentRunId, currentArtifact?.id]);

  // Lock body scroll while open
  useEffect(() => {
    if (!current) return;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prevOverflow;
    };
  }, [current]);

  // Fetch text content for non-image, non-PDF artifacts
  useEffect(() => {
    if (!current || !isTextLike) {
      setTextState({ loading: false, content: null, binary: false, error: null });
      return;
    }
    const controller = new AbortController();
    setTextState({ loading: true, content: null, binary: false, error: null });

    fetch(currentUrl, { signal: controller.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const contentType = res.headers.get('content-type') || '';
        if (
          isBinaryContentType(
            contentType,
            currentArtifact?.file_type ?? '',
            currentArtifact?.file_path ?? ''
          )
        ) {
          setTextState({ loading: false, content: null, binary: true, error: null });
          return;
        }
        const text = await res.text();
        // Quick heuristic for binary payload containing NUL bytes
        if (text.slice(0, 2000).includes('\u0000')) {
          setTextState({ loading: false, content: null, binary: true, error: null });
          return;
        }
        const truncated =
          text.length > MAX_TEXT_PREVIEW_CHARS
            ? `${text.slice(0, MAX_TEXT_PREVIEW_CHARS)}\n\n... (内容过长已截断，请点击右上角「下载」查看完整文件)`
            : text;
        setTextState({ loading: false, content: truncated, binary: false, error: null });
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setTextState({
          loading: false,
          content: null,
          binary: false,
          error: err instanceof Error ? err.message : '无法加载产物内容',
        });
      });

    return () => controller.abort();
  }, [current, isTextLike, currentUrl]);

  // Keyboard navigation & shortcuts
  useEffect(() => {
    if (!current) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        onClose();
      } else if (e.key === 'ArrowLeft' && hasMultiple) {
        e.preventDefault();
        handlePrev();
      } else if (e.key === 'ArrowRight' && hasMultiple) {
        e.preventDefault();
        handleNext();
      } else if ((e.key === '+' || e.key === '=') && !isPdf) {
        e.preventDefault();
        handleZoomIn();
      } else if (e.key === '-' && !isPdf) {
        e.preventDefault();
        handleZoomOut();
      } else if (e.key === '0' && !isPdf) {
        e.preventDefault();
        handleResetView();
      }
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [
    current,
    hasMultiple,
    isPdf,
    onClose,
    handlePrev,
    handleNext,
    handleZoomIn,
    handleZoomOut,
    handleResetView,
  ]);

  // Non-passive wheel listener for smooth image zooming
  useEffect(() => {
    const el = viewportRef.current;
    if (!el || !current || !isImage) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const factor = Math.exp(-e.deltaY * 0.002);
      setScale((prev) => {
        const next = clampScale(prev * factor);
        if (next <= 1) {
          setOffset({ x: 0, y: 0 });
        }
        return next;
      });
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [current, isImage]);

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!isImage || e.button !== 0) return;
    if (scale <= 1 && offset.x === 0 && offset.y === 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    dragStartRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      origX: offset.x,
      origY: offset.y,
      moved: false,
    };
    setIsDragging(true);
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragStartRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    if (Math.abs(dx) > 4 || Math.abs(dy) > 4) {
      drag.moved = true;
    }
    setOffset({ x: drag.origX + dx, y: drag.origY + dy });
  };

  const handlePointerUpOrCancel = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragStartRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    if (drag.moved) {
      suppressBackdropClickRef.current = true;
      window.setTimeout(() => {
        suppressBackdropClickRef.current = false;
      }, 0);
    }
    dragStartRef.current = null;
    setIsDragging(false);
  };

  const handleImageDoubleClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (scale === 1) {
      setScale(2);
    } else {
      setScale(1);
      setOffset({ x: 0, y: 0 });
    }
  };

  const handleCopyText = async () => {
    if (!textState.content) return;
    try {
      await navigator.clipboard.writeText(textState.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard API unavailable
    }
  };

  if (!current || !currentArtifact) return null;

  const fileName = currentArtifact.file_path.split('/').pop() || currentArtifact.file_path;
  const displayTitle = currentArtifact.description || fileName;
  const zoomPercent = Math.round(scale * 100);
  const canDragImage = isImage && (scale > 1 || offset.x !== 0 || offset.y !== 0);

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`预览产物 ${displayTitle}`}
      className="fixed inset-0 z-50 flex flex-col bg-[#161513]/88 backdrop-blur-sm text-[#F5F2EC] select-none"
      onClick={(e) => {
        if (e.target === e.currentTarget && !suppressBackdropClickRef.current) {
          onClose();
        }
      }}
    >
      {/* Top Toolbar */}
      <div
        className="shrink-0 flex items-center justify-between gap-3 px-4 py-2.5 bg-[#1F1E1C]/95 border-b border-white/10"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 min-w-0">
          {getArtifactBadgeIcon(currentArtifact.file_type, currentArtifact.file_path)}
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span
                className="text-sm font-medium text-[#FAF8F5] truncate"
                title={displayTitle}
              >
                {displayTitle}
              </span>
              {hasMultiple && (
                <span className="text-[11px] font-mono px-1.5 py-0.5 rounded-md bg-white/10 text-[#CFC9C0] shrink-0 tabular-nums">
                  {activeIndex + 1} / {items.length}
                </span>
              )}
            </div>
            <div
              className="text-[11px] font-mono text-[#A39B91] truncate"
              title={currentArtifact.file_path}
            >
              {currentArtifact.file_path}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {!isPdf && (
            <div className="inline-flex items-center rounded-lg bg-white/[0.07] border border-white/10 p-0.5">
              <button
                type="button"
                onClick={handleZoomOut}
                disabled={scale <= MIN_SCALE}
                title="缩小 (-)"
                aria-label="缩小"
                className="p-1.5 rounded-md text-[#EAE6DF] hover:bg-white/10 disabled:opacity-35 transition-colors"
              >
                <ZoomOut className="w-4 h-4" />
              </button>
              <button
                type="button"
                onClick={handleResetView}
                title="点击重置为 100% (0)"
                className="px-2 py-1 text-xs font-mono text-[#EAE6DF] hover:bg-white/10 rounded-md min-w-[52px] text-center tabular-nums transition-colors"
              >
                {zoomPercent}%
              </button>
              <button
                type="button"
                onClick={handleZoomIn}
                disabled={scale >= MAX_SCALE}
                title="放大 (+)"
                aria-label="放大"
                className="p-1.5 rounded-md text-[#EAE6DF] hover:bg-white/10 disabled:opacity-35 transition-colors"
              >
                <ZoomIn className="w-4 h-4" />
              </button>
              {isImage && (
                <>
                  <div className="w-px h-4 bg-white/10 mx-0.5" />
                  <button
                    type="button"
                    onClick={handleRotate}
                    title="顺时针旋转 90°"
                    aria-label="旋转图片"
                    className="p-1.5 rounded-md text-[#EAE6DF] hover:bg-white/10 transition-colors"
                  >
                    <RotateCw className="w-4 h-4" />
                  </button>
                </>
              )}
              {(scale !== 1 || rotation !== 0 || offset.x !== 0 || offset.y !== 0) && (
                <button
                  type="button"
                  onClick={handleResetView}
                  title="重置视图 (0)"
                  aria-label="重置视图"
                  className="p-1.5 rounded-md text-[#EAE6DF] hover:bg-white/10 transition-colors"
                >
                  <RotateCcw className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          )}

          {isTextLike && textState.content !== null && (
            <button
              type="button"
              onClick={() => void handleCopyText()}
              title="复制文本内容"
              className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-white/[0.08] hover:bg-white/15 border border-white/10 text-[#FAF8F5] transition-colors"
            >
              {copied ? (
                <>
                  <Check className="w-3.5 h-3.5 text-emerald-400" />
                  <span>已复制</span>
                </>
              ) : (
                <>
                  <Copy className="w-3.5 h-3.5" />
                  <span className="hidden sm:inline">复制</span>
                </>
              )}
            </button>
          )}

          <a
            href={`${currentUrl}?download=true`}
            download
            title="下载文件"
            className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-[#5B7A95] hover:bg-[#4B6882] text-white transition-colors"
          >
            <Download className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">下载</span>
          </a>

          <button
            type="button"
            onClick={onClose}
            title="关闭预览 (Esc)"
            aria-label="关闭预览"
            className="p-1.5 rounded-lg text-[#CFC9C0] hover:text-white hover:bg-white/15 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
      </div>

      {/* Main Preview Viewport */}
      <div
        ref={viewportRef}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUpOrCancel}
        onPointerCancel={handlePointerUpOrCancel}
        onClick={(e) => {
          if (e.target === e.currentTarget && !suppressBackdropClickRef.current) {
            onClose();
          }
        }}
        className={`relative flex-1 min-h-0 flex items-center justify-center overflow-hidden p-4 sm:p-8 ${
          canDragImage ? (isDragging ? 'cursor-grabbing' : 'cursor-grab') : ''
        }`}
      >
        {/* Previous / Next Navigation Buttons */}
        {hasMultiple && (
          <>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                handlePrev();
              }}
              title="上一个产物 (←)"
              aria-label="上一个产物"
              className="absolute left-3 sm:left-5 top-1/2 -translate-y-1/2 z-20 w-10 h-10 rounded-full bg-[#1F1E1C]/80 hover:bg-[#1F1E1C] border border-white/15 text-[#FAF8F5] flex items-center justify-center shadow-lg transition-all"
            >
              <ChevronLeft className="w-5 h-5" />
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                handleNext();
              }}
              title="下一个产物 (→)"
              aria-label="下一个产物"
              className="absolute right-3 sm:right-5 top-1/2 -translate-y-1/2 z-20 w-10 h-10 rounded-full bg-[#1F1E1C]/80 hover:bg-[#1F1E1C] border border-white/15 text-[#FAF8F5] flex items-center justify-center shadow-lg transition-all"
            >
              <ChevronRight className="w-5 h-5" />
            </button>
          </>
        )}

        {/* Image Preview */}
        {isImage && (
          <>
            {imageLoading && !imageError && (
              <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                <div className="inline-flex items-center gap-2 px-3.5 py-2 rounded-xl bg-[#1F1E1C]/90 border border-white/10 text-xs text-[#CFC9C0]">
                  <Loader2 className="w-4 h-4 animate-spin text-[#7DA2C2]" />
                  <span>正在加载图片...</span>
                </div>
              </div>
            )}
            {imageError ? (
              <div
                onClick={(e) => e.stopPropagation()}
                className="rounded-2xl bg-[#242220] border border-white/10 px-6 py-5 text-center max-w-sm"
              >
                <p className="text-sm text-[#EAE6DF] font-medium">图片加载失败</p>
                <p className="text-xs text-[#A39B91] mt-1">
                  文件可能已移动或格式不受浏览器支持，可尝试下载到本地查看。
                </p>
                <a
                  href={`${currentUrl}?download=true`}
                  download
                  className="mt-4 inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-medium bg-[#5B7A95] hover:bg-[#4B6882] text-white transition-colors"
                >
                  <Download className="w-3.5 h-3.5" />
                  <span>下载文件</span>
                </a>
              </div>
            ) : (
              <img
                src={currentUrl}
                alt={displayTitle}
                draggable={false}
                onLoad={() => setImageLoading(false)}
                onError={() => {
                  setImageLoading(false);
                  setImageError(true);
                }}
                onDoubleClick={handleImageDoubleClick}
                onClick={(e) => e.stopPropagation()}
                style={{
                  transform: `translate3d(${offset.x}px, ${offset.y}px, 0) scale(${scale}) rotate(${rotation}deg)`,
                  transition: isDragging ? 'none' : 'transform 140ms ease-out',
                }}
                className={`max-w-full max-h-full object-contain rounded-lg shadow-2xl bg-white/95 ${
                  imageLoading ? 'opacity-0' : 'opacity-100'
                }`}
              />
            )}
          </>
        )}

        {/* PDF Preview */}
        {isPdf && (
          <div
            onClick={(e) => e.stopPropagation()}
            className="w-full max-w-5xl h-full rounded-xl overflow-hidden border border-white/15 bg-white shadow-2xl"
          >
            <iframe
              src={currentUrl}
              title={displayTitle}
              className="w-full h-full border-0"
            />
          </div>
        )}

        {/* Text / Code Preview */}
        {isTextLike && (
          <div
            onClick={(e) => e.stopPropagation()}
            className="w-full max-w-4xl max-h-full flex flex-col rounded-2xl overflow-hidden border border-[#3A3733] bg-[#1E1D1B] shadow-2xl select-text"
          >
            {textState.loading ? (
              <div className="p-10 flex items-center justify-center gap-2 text-xs text-[#CFC9C0]">
                <Loader2 className="w-4 h-4 animate-spin text-[#7DA2C2]" />
                <span>正在加载产物内容...</span>
              </div>
            ) : textState.binary ? (
              <div className="p-8 text-center">
                <p className="text-sm font-medium text-[#FAF8F5]">
                  暂不支持在线预览此二进制文件
                </p>
                <p className="text-xs text-[#A39B91] mt-1">
                  请点击下方按钮下载到本地应用查看。
                </p>
                <a
                  href={`${currentUrl}?download=true`}
                  download
                  className="mt-4 inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-medium bg-[#5B7A95] hover:bg-[#4B6882] text-white transition-colors"
                >
                  <Download className="w-3.5 h-3.5" />
                  <span>下载文件</span>
                </a>
              </div>
            ) : textState.error ? (
              <div className="p-8 text-center">
                <p className="text-sm font-medium text-rose-300">无法预览产物内容</p>
                <p className="text-xs text-[#A39B91] mt-1">{textState.error}</p>
              </div>
            ) : (
              <div className="overflow-auto p-4 sm:p-5 kiki-terminal-scroll">
                <pre
                  style={{ fontSize: `${Math.round(13 * scale * 10) / 10}px` }}
                  className="font-mono text-[#EAE6DF] whitespace-pre-wrap break-words leading-relaxed"
                >
                  {textState.content ?? ''}
                </pre>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Bottom Shortcut Hint */}
      <div
        className="shrink-0 px-4 py-1.5 text-center text-[11px] text-[#A39B91] bg-[#1F1E1C]/80 border-t border-white/5"
        onClick={(e) => e.stopPropagation()}
      >
        {isImage
          ? `滚轮缩放 · 双击快速放大/还原 · 放大后可拖拽平移${hasMultiple ? ' · ←/→ 切换' : ''} · Esc 关闭`
          : isTextLike
            ? `+/- 缩放字号 · 0 重置${hasMultiple ? ' · ←/→ 切换' : ''} · Esc 关闭`
            : `${hasMultiple ? '←/→ 切换 · ' : ''}Esc 关闭`}
      </div>
    </div>,
    document.body
  );
};

export default ArtifactPreviewModal;
