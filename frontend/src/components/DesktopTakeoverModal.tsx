import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, ExternalLink, Loader2, XCircle } from 'lucide-react';
import {
  buildDesktopWsUrl,
  endDesktopSession,
  fetchDesktopClipboard,
  fetchDesktopStatus,
  setDesktopClipboard,
  startDesktopSession,
  updateDesktopSession,
} from '../api';
import type {
  DesktopControlMode,
  DesktopSession,
  DesktopStatusResponse,
  DesktopTakeoverContext,
  ResumeRunPayload,
} from '../types';
import DesktopToolbar from './DesktopToolbar';

export interface DesktopTakeoverModalProps {
  open: boolean;
  context?: DesktopTakeoverContext | null;
  onClose: () => void;
  onResumeRun?: (runId: string, payload: ResumeRunPayload) => Promise<void> | void;
}

export const DesktopTakeoverModal: React.FC<DesktopTakeoverModalProps> = ({
  open,
  context,
  onClose,
  onResumeRun,
}) => {
  const [status, setStatus] = useState<DesktopStatusResponse | null>(null);
  const [session, setSession] = useState<DesktopSession | null>(null);
  const [connected, setConnected] = useState(false);
  const [frameSrc, setFrameSrc] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [handoffNote, setHandoffNote] = useState('');
  const [handoffBusy, setHandoffBusy] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const lastMoveSentRef = useRef<number>(0);

  const sendWs = useCallback((msg: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }, []);

  const cleanupSession = useCallback(async () => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setConnected(false);
    const sid = sessionIdRef.current;
    sessionIdRef.current = null;
    if (sid) {
      try {
        await endDesktopSession(sid);
      } catch {
        // ignore if already ended
      }
    }
    setSession(null);
    setFrameSrc(null);
  }, []);

  useEffect(() => {
    if (!open) {
      void cleanupSession();
      return;
    }
    let cancelled = false;
    setLoading(true);
    setErrorMsg(null);
    setHandoffNote('');

    (async () => {
      try {
        const st = await fetchDesktopStatus();
        if (cancelled) return;
        setStatus(st);
        if (!st.available) {
          setErrorMsg(st.error || st.driver_error || '当前服务器不支持桌面接管');
          setLoading(false);
          return;
        }
        const sess = await startDesktopSession({
          control_mode: st.supports_control ? 'control' : 'view',
          run_id: context?.runId,
          topic_id: context?.topicId,
          reason: context?.reason,
          instructions: context?.instructions ?? undefined,
          force: true,
        });
        if (cancelled) {
          await endDesktopSession(sess.id).catch(() => {});
          return;
        }
        sessionIdRef.current = sess.id;
        setSession(sess);

        const ws = new WebSocket(buildDesktopWsUrl(sess.id, sess.ws_token || ''));
        wsRef.current = ws;
        ws.onopen = () => !cancelled && setConnected(true);
        ws.onclose = () => !cancelled && setConnected(false);
        ws.onerror = () => !cancelled && setErrorMsg('桌面流 WebSocket 连接异常，请确认后端服务正常运行。');
        ws.onmessage = (ev) => {
          if (cancelled) return;
          try {
            const msg = JSON.parse(ev.data as string);
            if (msg.type === 'frame' && msg.data) {
              setFrameSrc(`data:${msg.mime_type || 'image/jpeg'};base64,${msg.data}`);
              setLoading(false);
            } else if (msg.type === 'session' && msg.session) {
              setSession((prev) => (prev ? { ...prev, ...msg.session } : msg.session));
            } else if (msg.type === 'error' && msg.message) {
              setErrorMsg(String(msg.message));
            }
          } catch {
            // ignore malformed messages
          }
        };
      } catch (err) {
        if (!cancelled) {
          setErrorMsg(err instanceof Error ? err.message : '启动桌面接管会话失败');
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
      void cleanupSession();
    };
  }, [open, context?.runId, context?.topicId, context?.reason, context?.instructions, cleanupSession]);

  const mapPointerCoords = (e: React.MouseEvent<HTMLImageElement>): { x: number; y: number } | null => {
    const img = imgRef.current;
    if (!img) return null;
    const rect = img.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    return { x, y };
  };

  const mapButton = (btn: number): 'left' | 'right' | 'middle' =>
    btn === 2 ? 'right' : btn === 1 ? 'middle' : 'left';

  const canControl = Boolean(session && session.control_mode === 'control' && connected);

  const handleMouseMove = (e: React.MouseEvent<HTMLImageElement>) => {
    if (!canControl) return;
    const now = performance.now();
    if (now - lastMoveSentRef.current < 45) return;
    lastMoveSentRef.current = now;
    const pt = mapPointerCoords(e);
    if (pt) sendWs({ type: 'input', payload: { action: 'mouse_move', ...pt } });
  };

  const handleMouseDown = (e: React.MouseEvent<HTMLImageElement>) => {
    if (!canControl) return;
    e.preventDefault();
    (e.currentTarget as HTMLElement).focus();
    const pt = mapPointerCoords(e);
    if (pt) sendWs({ type: 'input', payload: { action: 'mouse_down', ...pt, button: mapButton(e.button) } });
  };

  const handleMouseUp = (e: React.MouseEvent<HTMLImageElement>) => {
    if (!canControl) return;
    e.preventDefault();
    const pt = mapPointerCoords(e);
    if (pt) sendWs({ type: 'input', payload: { action: 'mouse_up', ...pt, button: mapButton(e.button) } });
  };

  const handleDoubleClick = (e: React.MouseEvent<HTMLImageElement>) => {
    if (!canControl) return;
    e.preventDefault();
    const pt = mapPointerCoords(e);
    if (pt) sendWs({ type: 'input', payload: { action: 'mouse_double_click', ...pt, button: 'left' } });
  };

  const handleWheel = (e: React.WheelEvent<HTMLImageElement>) => {
    if (!canControl) return;
    sendWs({
      type: 'input',
      payload: { action: 'mouse_scroll', delta_x: -Math.round(e.deltaX / 3), delta_y: -Math.round(e.deltaY / 3) },
    });
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!canControl) return;
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
    e.preventDefault();
    const modifiers: string[] = [];
    if (e.metaKey) modifiers.push('meta');
    if (e.ctrlKey) modifiers.push('ctrl');
    if (e.altKey) modifiers.push('alt');
    if (e.shiftKey) modifiers.push('shift');
    if (['Meta', 'Control', 'Alt', 'Shift'].includes(e.key)) return;
    sendWs({ type: 'input', payload: { action: 'key_tap', key: e.key, modifiers } });
  };

  const handleUpdateConfig = async (patch: { control_mode?: DesktopControlMode; fps?: number; display_index?: number }) => {
    if (!session) return;
    try {
      const updated = await updateDesktopSession(session.id, patch);
      setSession((prev) => (prev ? { ...prev, ...updated } : updated));
      sendWs({ type: 'refresh' });
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : '更新桌面配置失败');
    }
  };

  const handleHandoff = async (approved: boolean) => {
    if (!context?.runId || !onResumeRun || handoffBusy) return;
    setHandoffBusy(true);
    try {
      await cleanupSession();
      await onResumeRun(context.runId, {
        decision: approved ? 'takeover_done' : 'takeover_cancel',
        approved,
        comment: handoffNote.trim() || undefined,
      });
      onClose();
    } finally {
      setHandoffBusy(false);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 backdrop-blur-sm p-3 sm:p-5">
      <div
        className={`flex flex-col rounded-2xl bg-[#FDFBF8] border border-[#DFD8CC] shadow-2xl overflow-hidden transition-all ${
          isFullscreen ? 'w-full h-full' : 'w-full max-w-5xl h-[86vh]'
        }`}
      >
        <DesktopToolbar
          session={session}
          connected={connected}
          supportsControl={Boolean(status?.supports_control)}
          supportsClipboard={Boolean(status?.supports_clipboard)}
          displays={status?.displays ?? []}
          isFullscreen={isFullscreen}
          onToggleMode={(mode) => void handleUpdateConfig({ control_mode: mode })}
          onChangeFps={(fps) => void handleUpdateConfig({ fps })}
          onChangeDisplay={(display_index) => void handleUpdateConfig({ display_index })}
          onRefresh={() => sendWs({ type: 'refresh' })}
          onSendShortcut={(key, modifiers = []) => sendWs({ type: 'input', payload: { action: 'key_combo', key, modifiers } })}
          onSendText={(text) => sendWs({ type: 'input', payload: { action: 'type_text', text } })}
          onSyncClipboardFromServer={() => {
            if (!session) return;
            fetchDesktopClipboard(session.id)
              .then((text) => navigator.clipboard?.writeText(text).catch(() => {}))
              .catch(() => {});
          }}
          onPushClipboardToServer={(text) => {
            if (!session) return;
            void setDesktopClipboard(session.id, text).catch(() => {});
          }}
          onToggleFullscreen={() => setIsFullscreen((v) => !v)}
          onClose={onClose}
        />

        {/* Permission Warnings / Error Banner */}
        {(errorMsg || (status?.permissions.warnings.length ?? 0) > 0) && (
          <div className="bg-amber-50 border-b border-amber-200 px-4 py-2 text-xs text-amber-900 space-y-1 shrink-0">
            {errorMsg && (
              <div className="flex items-center gap-1.5 font-medium text-rose-700">
                <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
                <span>{errorMsg}</span>
              </div>
            )}
            {status?.permissions.warnings.map((w, idx) => (
              <div key={idx} className="flex items-center gap-1.5">
                <AlertTriangle className="w-3.5 h-3.5 shrink-0 text-amber-600" />
                <span>{w}</span>
              </div>
            ))}
          </div>
        )}

        {/* Interactive Remote Desktop Canvas */}
        <div
          tabIndex={0}
          onKeyDown={handleKeyDown}
          className="flex-1 bg-[#181716] flex items-center justify-center overflow-hidden relative focus:outline-none select-none"
        >
          {loading && !frameSrc ? (
            <div className="flex flex-col items-center gap-2 text-[#D6CFC5] text-xs">
              <Loader2 className="w-6 h-6 animate-spin text-[#D97706]" />
              <span>正在连接 Kiki 服务器桌面画面...</span>
            </div>
          ) : frameSrc ? (
            <img
              ref={imgRef}
              src={frameSrc}
              alt="Kiki Server Desktop"
              draggable={false}
              onContextMenu={(e) => e.preventDefault()}
              onMouseMove={handleMouseMove}
              onMouseDown={handleMouseDown}
              onMouseUp={handleMouseUp}
              onDoubleClick={handleDoubleClick}
              onWheel={handleWheel}
              className={`max-w-full max-h-full object-contain ${
                canControl ? 'cursor-crosshair' : 'cursor-default'
              }`}
            />
          ) : (
            <div className="text-xs text-[#A8A095]">暂无桌面画面</div>
          )}
        </div>

        {/* HITL Handoff Footer (shown when launched from a waiting SessionRun) */}
        {context?.runId && onResumeRun && (
          <div className="border-t border-amber-300 bg-amber-50/90 px-4 py-3 flex flex-col sm:flex-row sm:items-center gap-2.5 shrink-0">
            <div className="flex-1 min-w-0 space-y-1">
              <div className="text-xs font-semibold text-amber-950">
                Kiki 请求接管：{context.reason || '请完成手动操作后交还给 Kiki'}
              </div>
              {context.instructions && (
                <div className="text-[11px] text-amber-900/90">{context.instructions}</div>
              )}
              {context.url && (
                <a
                  href={context.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-[11px] text-[#456B8A] hover:underline"
                >
                  <ExternalLink className="w-3 h-3" />
                  {context.url}
                </a>
              )}
              <input
                type="text"
                value={handoffNote}
                onChange={(e) => setHandoffNote(e.target.value)}
                disabled={handoffBusy}
                placeholder="可选：补充说明给 Kiki（例如“已完成登录与验证码”）"
                className="w-full mt-1 px-2.5 py-1 rounded-lg text-xs bg-white border border-amber-300 text-[#262422] focus:outline-none focus:border-amber-500"
              />
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <button
                type="button"
                disabled={handoffBusy}
                onClick={() => void handleHandoff(false)}
                className="inline-flex items-center gap-1 px-3 py-2 rounded-xl text-xs font-semibold bg-white hover:bg-rose-50 text-rose-700 border border-rose-300 disabled:opacity-50"
              >
                <XCircle className="w-3.5 h-3.5" />
                无法处理
              </button>
              <button
                type="button"
                disabled={handoffBusy}
                onClick={() => void handleHandoff(true)}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs font-semibold bg-[#D97706] hover:bg-[#B45309] text-white shadow-sm disabled:opacity-50"
              >
                <CheckCircle2 className="w-3.5 h-3.5" />
                我已处理完成，交还给 Kiki
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default DesktopTakeoverModal;
