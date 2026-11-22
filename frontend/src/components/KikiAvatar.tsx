import React from 'react';
import type { KikiStatus } from '../types';

export interface KikiAvatarProps {
  status: KikiStatus;
  size?: 'xs' | 'sm' | 'md' | 'lg' | 'xl' | 'hero';
  className?: string;
  showLabel?: boolean;
}

const SIZE_CLASSES: Record<NonNullable<KikiAvatarProps['size']>, string> = {
  xs: 'w-5 h-5',
  sm: 'w-9 h-9',
  md: 'w-14 h-14',
  lg: 'w-20 h-20',
  xl: 'w-24 h-24',
  hero: 'w-36 h-36',
};

const SPRITE_SRC: Record<KikiStatus, string> = {
  idle: '/kiki-idle.png',
  thinking: '/kiki-thinking.png',
  running: '/kiki-running.png',
  waiting_input: '/kiki-waiting_input.png',
};

const ANIMATION_CLASS: Record<KikiStatus, string> = {
  idle: 'animate-kiki-breathe origin-bottom',
  thinking: 'animate-kiki-think origin-bottom',
  running: 'animate-kiki-run origin-bottom',
  waiting_input: 'animate-kiki-wave origin-bottom',
};

export const KIKI_STATUS_LABELS: Record<KikiStatus, string> = {
  idle: '揣手手待机中',
  thinking: '歪头思考分流中...',
  running: '戴帽跑腿干活中...',
  waiting_input: '举爪等你拍板！',
};

export const KikiAvatar: React.FC<KikiAvatarProps> = ({
  status,
  size = 'md',
  className = '',
  showLabel = false,
}) => {
  const isLarge = size === 'lg' || size === 'xl' || size === 'hero';

  const sprite = (
    <div
      role="img"
      aria-label={`Kiki: ${KIKI_STATUS_LABELS[status]}`}
      title={KIKI_STATUS_LABELS[status]}
      className={`relative shrink-0 select-none inline-flex items-center justify-center ${SIZE_CLASSES[size]} ${className}`}
    >
      {/* Subtle ground shadow for larger companion/stage sizes */}
      {isLarge && (
        <div
          className={`absolute bottom-1 left-1/2 -translate-x-1/2 w-[68%] h-2.5 rounded-full bg-[#B8ACA0]/45 blur-[3px] pointer-events-none ${status === 'running' ? 'animate-kiki-shadow' : ''
            }`}
        />
      )}

      {/* Ambient motion accents for large sprite states */}
      {isLarge && status === 'thinking' && (
        <span className="absolute -top-0.5 right-2 w-2.5 h-2.5 rounded-full bg-[#5B7A95]/55 animate-ping pointer-events-none" />
      )}
      {isLarge && status === 'waiting_input' && (
        <span className="absolute top-0 left-1/2 -translate-x-1/2 w-5 h-5 rounded-full bg-amber-400/35 animate-ping pointer-events-none" />
      )}
      {isLarge && status === 'running' && (
        <div className="absolute left-0 bottom-5 flex flex-col gap-1 pointer-events-none">
          <span className="w-3.5 h-0.5 rounded-full bg-[#8C8378]/50 animate-kiki-dash" />
          <span className="w-2.5 h-0.5 rounded-full bg-[#8C8378]/40 animate-kiki-dash [animation-delay:250ms]" />
        </div>
      )}

      <img
        src={SPRITE_SRC[status]}
        alt={`Kiki ${status}`}
        draggable={false}
        className={`relative z-10 w-full h-full object-contain drop-shadow-[0_2px_4px_rgba(38,36,34,0.08)] transition-opacity duration-200 ${ANIMATION_CLASS[status]}`}
      />
    </div>
  );

  if (!showLabel) {
    return sprite;
  }

  return (
    <div className="inline-flex items-center gap-2">
      {sprite}
      <span className="text-xs font-medium text-[#5A534C]">
        {KIKI_STATUS_LABELS[status]}
      </span>
    </div>
  );
};

export default KikiAvatar;
