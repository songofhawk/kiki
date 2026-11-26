import React from 'react';
import { X, ArrowUpRight } from 'lucide-react';
import type { ToastNotification } from '../types';
import KikiAvatar from './KikiAvatar';

export interface ToastContainerProps {
  toasts: ToastNotification[];
  onSelectTopic: (topicId: string) => void;
  onDismiss: (toastId: string) => void;
}

export const ToastContainer: React.FC<ToastContainerProps> = ({
  toasts,
  onSelectTopic,
  onDismiss,
}) => {
  if (toasts.length === 0) return null;

  return (
    <div className="fixed top-4 right-4 z-50 flex flex-col gap-2.5 max-w-sm w-[calc(100vw-2rem)] pointer-events-none">
      {toasts.map((toast, idx) => (
        <div
          key={toast.id}
          onClick={() => {
            onSelectTopic(toast.topic_id);
            onDismiss(toast.id);
          }}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              onSelectTopic(toast.topic_id);
              onDismiss(toast.id);
            }
          }}
          className="pointer-events-auto group cursor-pointer rounded-2xl bg-white/95 backdrop-blur-md border border-[#E4DDD2] shadow-lg shadow-stone-900/10 p-3.5 flex items-start gap-3 hover:border-[#5B7A95] transition-all animate-toast-in"
        >
          <div className="w-11 h-11 rounded-2xl bg-[#EEF4F8] border border-[#D4E2ED] flex items-center justify-center shrink-0 -my-0.5">
            <KikiAvatar
              status="running"
              pose="delivering"
              animated={idx === 0}
              size="sm"
            />
          </div>

          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-1.5 text-xs font-semibold text-[#262422]">
              <span className="text-[#5B7A95] truncate">#{toast.topic_title}</span>
              <span className="shrink-0">捷报送达</span>
              <ArrowUpRight className="w-3.5 h-3.5 text-[#8C8378] opacity-0 group-hover:opacity-100 transition-opacity shrink-0" />
            </div>
            <p className="mt-0.5 text-xs text-[#5C554E] break-words leading-relaxed">
              {toast.summary}
            </p>
          </div>

          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onDismiss(toast.id);
            }}
            aria-label="关闭通知"
            className="p-1 rounded-lg text-[#9A9187] hover:text-[#262422] hover:bg-[#FAF8F5] transition-colors shrink-0"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
};

export default ToastContainer;
