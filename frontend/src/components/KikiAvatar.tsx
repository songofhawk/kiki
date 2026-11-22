import React from 'react';
import type { KikiStatus } from '../types';

export interface KikiAvatarProps {
  status: KikiStatus;
  size?: 'xs' | 'sm' | 'md' | 'lg' | 'xl';
  className?: string;
  showLabel?: boolean;
}

const SIZE_CLASSES: Record<NonNullable<KikiAvatarProps['size']>, string> = {
  xs: 'w-5 h-5',
  sm: 'w-9 h-9',
  md: 'w-12 h-12',
  lg: 'w-16 h-16',
  xl: 'w-20 h-20',
};

const SPRITE_POSITION: Record<KikiStatus, string> = {
  idle: '0% 0%',
  thinking: '100% 0%',
  running: '0% 100%',
  waiting_input: '100% 100%',
};

const ANIMATION_CLASS: Record<KikiStatus, string> = {
  idle: 'animate-kiki-breathe',
  thinking: 'animate-kiki-think',
  running: 'animate-kiki-run',
  waiting_input: 'animate-kiki-wave',
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
  const sprite = (
    <div
      role="img"
      aria-label={`Kiki: ${KIKI_STATUS_LABELS[status]}`}
      title={KIKI_STATUS_LABELS[status]}
      className={`relative shrink-0 select-none mix-blend-multiply transition-transform duration-300 ${SIZE_CLASSES[size]} ${ANIMATION_CLASS[status]} ${className}`}
      style={{
        backgroundImage: 'url(/kiki-states.png)',
        backgroundSize: '200% 200%',
        backgroundRepeat: 'no-repeat',
        backgroundPosition: SPRITE_POSITION[status],
      }}
    />
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
