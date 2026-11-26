import React, { useState } from 'react';
import {
  ClipboardCopy,
  ClipboardPaste,
  Eye,
  Hand,
  Maximize2,
  Minimize2,
  Monitor,
  RefreshCw,
  Send,
  X,
} from 'lucide-react';
import type {
  DesktopControlMode,
  DesktopDisplayInfo,
  DesktopSession,
} from '../types';

export interface DesktopToolbarProps {
  session: DesktopSession | null;
  connected: boolean;
  supportsControl: boolean;
  supportsClipboard: boolean;
  displays: DesktopDisplayInfo[];
  isFullscreen: boolean;
  onToggleMode: (mode: DesktopControlMode) => void;
  onChangeFps: (fps: number) => void;
  onChangeDisplay: (displayIndex: number) => void;
  onRefresh: () => void;
  onSendShortcut: (key: string, modifiers?: string[]) => void;
  onSendText: (text: string) => void;
  onSyncClipboardFromServer: () => void;
  onPushClipboardToServer: (text: string) => void;
  onToggleFullscreen: () => void;
  onClose: () => void;
}

const SHORTCUTS: Array<{ label: string; key: string; modifiers?: string[] }> = [
  { label: '⌘C', key: 'c', modifiers: ['meta'] },
  { label: '⌘V', key: 'v', modifiers: ['meta'] },
  { label: '⌘A', key: 'a', modifiers: ['meta'] },
  { label: '⌘Tab', key: 'tab', modifiers: ['meta'] },
  { label: 'Esc', key: 'escape' },
  { label: 'Enter', key: 'enter' },
];

export const DesktopToolbar: React.FC<DesktopToolbarProps> = ({
  session,
  connected,
  supportsControl,
  supportsClipboard,
  displays,
  isFullscreen,
  onToggleMode,
  onChangeFps,
  onChangeDisplay,
  onRefresh,
  onSendShortcut,
  onSendText,
  onSyncClipboardFromServer,
  onPushClipboardToServer,
  onToggleFullscreen,
  onClose,
}) => {
  const [textInput, setTextInput] = useState('');
  const isControl = session?.control_mode === 'control';

  const handleSendText = (e: React.FormEvent) => {
    e.preventDefault();
    if (!textInput) return;
    onSendText(textInput);
    setTextInput('');
  };

  return (
    <div className="border-b border-[#E6DFD5] bg-[#FAF7F2] px-4 py-2.5 space-y-2 shrink-0">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="w-8 h-8 rounded-xl bg-[#262422] text-[#FAF7F2] flex items-center justify-center shrink-0">
            <Monitor className="w-4 h-4" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-[#262422] truncate">
                Kiki 服务器桌面接管
              </h2>
              <span
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium ${
                  connected
                    ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                    : 'bg-amber-50 text-amber-700 border border-amber-200'
                }`}
              >
                <span
                  className={`w-1.5 h-1.5 rounded-full ${
                    connected ? 'bg-emerald-500 animate-pulse' : 'bg-amber-500'
                  }`}
                />
                {connected ? '实时画面已连接' : '连接中...'}
              </span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-1.5 flex-wrap">
          {/* View / Control Mode Toggle */}
          <div className="inline-flex rounded-xl border border-[#DDD5C9] bg-white p-0.5">
            <button
              type="button"
              onClick={() => onToggleMode('view')}
              className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-medium transition-colors ${
                !isControl
                  ? 'bg-[#262422] text-white'
                  : 'text-[#6B635B] hover:text-[#262422]'
              }`}
            >
              <Eye className="w-3.5 h-3.5" />
              只读
            </button>
            <button
              type="button"
              disabled={!supportsControl}
              onClick={() => onToggleMode('control')}
              title={supportsControl ? '接管鼠标键盘控制' : '需先开启 macOS 辅助功能权限'}
              className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-medium transition-colors disabled:opacity-40 ${
                isControl
                  ? 'bg-[#D97706] text-white'
                  : 'text-[#6B635B] hover:text-[#262422]'
              }`}
            >
              <Hand className="w-3.5 h-3.5" />
              接管控制
            </button>
          </div>

          {/* Display Selector */}
          {displays.length > 1 && session && (
            <select
              value={session.display_index}
              onChange={(e) => onChangeDisplay(Number(e.target.value))}
              className="px-2 py-1 rounded-xl text-xs bg-white border border-[#DDD5C9] text-[#262422]"
            >
              {displays.map((d) => (
                <option key={d.index} value={d.index}>
                  {d.name} ({d.width}×{d.height})
                </option>
              ))}
            </select>
          )}

          {/* FPS Selector */}
          {session && (
            <select
              value={session.fps}
              onChange={(e) => onChangeFps(Number(e.target.value))}
              title="画面刷新帧率"
              className="px-2 py-1 rounded-xl text-xs bg-white border border-[#DDD5C9] text-[#262422]"
            >
              <option value={2}>2 FPS</option>
              <option value={4}>4 FPS</option>
              <option value={8}>8 FPS</option>
              <option value={12}>12 FPS</option>
            </select>
          )}

          <button
            type="button"
            onClick={onRefresh}
            title="立即刷新画面"
            className="p-1.5 rounded-xl bg-white border border-[#DDD5C9] text-[#5A534C] hover:bg-[#F3EFE9]"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>

          <button
            type="button"
            onClick={onToggleFullscreen}
            title={isFullscreen ? '退出全屏' : '放大窗口'}
            className="p-1.5 rounded-xl bg-white border border-[#DDD5C9] text-[#5A534C] hover:bg-[#F3EFE9]"
          >
            {isFullscreen ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
          </button>

          <button
            type="button"
            onClick={onClose}
            title="结束桌面接管并关闭"
            className="p-1.5 rounded-xl bg-white border border-[#DDD5C9] text-[#5A534C] hover:bg-rose-50 hover:text-rose-700 hover:border-rose-200"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Shortcuts + Unicode Text Input + Clipboard Sync */}
      {isControl && (
        <div className="flex items-center gap-2 flex-wrap pt-0.5">
          <div className="flex items-center gap-1 flex-wrap">
            {SHORTCUTS.map((sc) => (
              <button
                key={sc.label}
                type="button"
                onClick={() => onSendShortcut(sc.key, sc.modifiers)}
                className="px-2 py-1 rounded-lg text-[11px] font-mono bg-white hover:bg-[#EFEAE2] border border-[#DDD5C9] text-[#3B3631]"
              >
                {sc.label}
              </button>
            ))}
          </div>

          <form onSubmit={handleSendText} className="flex items-center gap-1 flex-1 min-w-[200px]">
            <input
              type="text"
              value={textInput}
              onChange={(e) => setTextInput(e.target.value)}
              placeholder="输入中文/验证码并发送到服务器光标处..."
              className="flex-1 px-2.5 py-1 rounded-lg text-xs bg-white border border-[#DDD5C9] text-[#262422] focus:outline-none focus:border-[#D97706]"
            />
            <button
              type="submit"
              disabled={!textInput}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-medium bg-[#262422] text-white hover:bg-[#3B3734] disabled:opacity-40"
            >
              <Send className="w-3 h-3" />
              发送
            </button>
          </form>

          {supportsClipboard && (
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={onSyncClipboardFromServer}
                title="读取服务器剪贴板到本地"
                className="inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] bg-white hover:bg-[#EFEAE2] border border-[#DDD5C9] text-[#5A534C]"
              >
                <ClipboardCopy className="w-3 h-3" />
                拉取剪贴板
              </button>
              <button
                type="button"
                onClick={() => {
                  if (textInput) {
                    onPushClipboardToServer(textInput);
                  } else if (navigator.clipboard?.readText) {
                    navigator.clipboard.readText().then((t) => t && onPushClipboardToServer(t)).catch(() => {});
                  }
                }}
                title="将输入框或本地剪贴板推送到服务器"
                className="inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] bg-white hover:bg-[#EFEAE2] border border-[#DDD5C9] text-[#5A534C]"
              >
                <ClipboardPaste className="w-3 h-3" />
                推送剪贴板
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default DesktopToolbar;
