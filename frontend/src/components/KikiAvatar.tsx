import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { KikiStatus } from '../types';

export type KikiPose =
  | 'idle'
  | 'idle_lazy'
  | 'idle_playful'
  | 'thinking'
  | 'working'
  | 'delivering'
  | 'waiting_input';

export interface KikiAvatarProps {
  status: KikiStatus;
  pose?: KikiPose;
  /**
   * Whether this Kiki instance plays multi-frame WebP & CSS motion.
   * Defaults to `false` so historical messages, brand marks, and badges stay calm & static.
   */
  animated?: boolean;
  /**
   * Enables periodic random lazy/playful actions (when `animated` is true)
   * and click-to-pet interaction when idle.
   */
  companion?: boolean;
  size?: 'xs' | 'sm' | 'md' | 'lg' | 'xl' | 'hero';
  className?: string;
  showLabel?: boolean;
  onCompanionMoodChange?: (moodLabel: string | null) => void;
}

const SIZE_CLASSES: Record<NonNullable<KikiAvatarProps['size']>, string> = {
  xs: 'w-5 h-5',
  sm: 'w-9 h-9',
  md: 'w-14 h-14',
  lg: 'w-20 h-20',
  xl: 'w-24 h-24',
  hero: 'w-36 h-36',
};

const ASSET_VER = 'v=20261122b';

const ANIMATED_WEBP_SRC: Record<KikiPose, string> = {
  idle: `/kiki/idle.webp?${ASSET_VER}`,
  idle_lazy: `/kiki/idle_lazy.webp?${ASSET_VER}`,
  idle_playful: `/kiki/idle_playful.webp?${ASSET_VER}`,
  thinking: `/kiki/thinking.webp?${ASSET_VER}`,
  working: `/kiki/working.webp?${ASSET_VER}`,
  delivering: `/kiki/delivering.webp?${ASSET_VER}`,
  waiting_input: `/kiki/waiting_input.webp?${ASSET_VER}`,
};

const STATIC_PNG_SRC: Record<KikiPose, string> = {
  idle: `/kiki/idle.png?${ASSET_VER}`,
  idle_lazy: `/kiki/idle_lazy.png?${ASSET_VER}`,
  idle_playful: `/kiki/idle_playful.png?${ASSET_VER}`,
  thinking: `/kiki/thinking.png?${ASSET_VER}`,
  working: `/kiki/working.png?${ASSET_VER}`,
  delivering: `/kiki/delivering.png?${ASSET_VER}`,
  waiting_input: `/kiki/waiting_input.png?${ASSET_VER}`,
};

const POSE_MOTION_CLASS: Record<KikiPose, string> = {
  idle: 'animate-kiki-breathe origin-bottom',
  idle_lazy: 'origin-bottom',
  idle_playful: 'origin-bottom',
  thinking: 'animate-kiki-think origin-bottom',
  working: 'origin-bottom',
  delivering: 'animate-kiki-run origin-center',
  waiting_input: 'animate-kiki-wave origin-bottom',
};

export const KIKI_STATUS_LABELS: Record<KikiStatus, string> = {
  idle: '乖巧坐等召唤中',
  thinking: '歪头思考分流中...',
  running: '戴帽敲键盘干活中...',
  waiting_input: '举爪等你拍板！',
};

export const KIKI_POSE_LABELS: Record<KikiPose, string> = {
  idle: '乖巧坐等召唤中',
  idle_lazy: '打哈欠犯困趴卧中...',
  idle_playful: '舔爪歪头调皮中~',
  thinking: '歪头思考分流中...',
  working: '戴帽敲键盘干活中...',
  delivering: '叼信飞奔派送中...',
  waiting_input: '举爪等你拍板！',
};

const LAZY_MOOD_QUIPS = [
  '打了个大哈欠，趴着打盹 zZ',
  '晒着纸面暖光，慵懒眯眼...',
  '揣手手犯困，随叫随醒哦~',
];

const PLAYFUL_MOOD_QUIPS = [
  '舔舔小肉垫，偷瞄你的屏幕~',
  '歪头晃尾巴，伸爪求击掌！',
  '喵呜~ 点点我随时帮你跑腿！',
];

function statusToDefaultPose(status: KikiStatus): KikiPose {
  switch (status) {
    case 'thinking':
      return 'thinking';
    case 'running':
      return 'working';
    case 'waiting_input':
      return 'waiting_input';
    case 'idle':
    default:
      return 'idle';
  }
}

export const KikiAvatar: React.FC<KikiAvatarProps> = ({
  status,
  pose,
  animated = false,
  companion = false,
  size = 'md',
  className = '',
  showLabel = false,
  onCompanionMoodChange,
}) => {
  const [ambientPose, setAmbientPose] = useState<'idle' | 'idle_lazy' | 'idle_playful'>('idle');
  const [ambientQuip, setAmbientQuip] = useState<string | null>(null);
  const [usePngFallback, setUsePngFallback] = useState(false);
  const revertTimerRef = useRef<number | null>(null);
  const tapToggleRef = useRef<boolean>(false);
  const onMoodRef = useRef(onCompanionMoodChange);
  onMoodRef.current = onCompanionMoodChange;

  const isCompanionIdle = companion && status === 'idle' && !pose;

  const triggerMoodBurst = useCallback(
    (nextPose: 'idle_lazy' | 'idle_playful', customQuip?: string) => {
      if (revertTimerRef.current !== null) {
        window.clearTimeout(revertTimerRef.current);
      }
      const quips = nextPose === 'idle_lazy' ? LAZY_MOOD_QUIPS : PLAYFUL_MOOD_QUIPS;
      const quip = customQuip ?? quips[Math.floor(Math.random() * quips.length)];
      setAmbientPose(nextPose);
      setAmbientQuip(quip);
      onMoodRef.current?.(quip);

      const durationMs = nextPose === 'idle_lazy' ? 5200 : 3600;
      revertTimerRef.current = window.setTimeout(() => {
        setAmbientPose('idle');
        setAmbientQuip(null);
        onMoodRef.current?.(null);
        revertTimerRef.current = null;
      }, durationMs);
    },
    []
  );

  useEffect(() => {
    // Only auto-schedule random ambient bursts when both `companion` AND `animated` are true
    if (!isCompanionIdle || !animated) {
      if (!isCompanionIdle) {
        if (revertTimerRef.current !== null) {
          window.clearTimeout(revertTimerRef.current);
          revertTimerRef.current = null;
        }
        setAmbientPose('idle');
        setAmbientQuip(null);
        onMoodRef.current?.(null);
      }
      return;
    }

    let scheduleTimer: number | null = null;
    const scheduleNext = (initial = false) => {
      const delay = initial
        ? 4500 + Math.random() * 3000
        : 9000 + Math.random() * 5500;
      scheduleTimer = window.setTimeout(() => {
        const pick: 'idle_lazy' | 'idle_playful' =
          Math.random() < 0.55 ? 'idle_playful' : 'idle_lazy';
        triggerMoodBurst(pick);
        scheduleNext(false);
      }, delay);
    };

    scheduleNext(true);

    return () => {
      if (scheduleTimer !== null) {
        window.clearTimeout(scheduleTimer);
      }
      if (revertTimerRef.current !== null) {
        window.clearTimeout(revertTimerRef.current);
        revertTimerRef.current = null;
      }
    };
  }, [isCompanionIdle, animated, triggerMoodBurst]);

  const resolvedPose: KikiPose = pose
    ? pose
    : isCompanionIdle
      ? ambientPose
      : statusToDefaultPose(status);

  // If a static companion (e.g. Welcome Hero) is clicked by the user, animate just that burst
  const effectiveAnimated = animated || (isCompanionIdle && ambientPose !== 'idle');

  useEffect(() => {
    setUsePngFallback(false);
  }, [resolvedPose, effectiveAnimated]);

  const labelText =
    (isCompanionIdle && ambientQuip) || KIKI_POSE_LABELS[resolvedPose] || KIKI_STATUS_LABELS[status];

  const isLarge = size === 'lg' || size === 'xl' || size === 'hero';
  const canPet = isCompanionIdle;

  const handleCompanionClick = () => {
    if (!canPet) return;
    tapToggleRef.current = !tapToggleRef.current;
    if (tapToggleRef.current) {
      triggerMoodBurst('idle_playful', '喵呜~ 伸爪和你击个掌！');
    } else {
      triggerMoodBurst('idle_lazy', '呼噜呼噜…揉脑门好犯困呀 zZ');
    }
  };

  const imgSrc =
    !effectiveAnimated || usePngFallback
      ? STATIC_PNG_SRC[resolvedPose]
      : ANIMATED_WEBP_SRC[resolvedPose];

  const motionClass = effectiveAnimated ? POSE_MOTION_CLASS[resolvedPose] : '';

  const sprite = (
    <div
      role={canPet ? 'button' : 'img'}
      tabIndex={canPet ? 0 : undefined}
      onClick={canPet ? handleCompanionClick : undefined}
      onKeyDown={
        canPet
          ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                handleCompanionClick();
              }
            }
          : undefined
      }
      aria-label={`Kiki: ${labelText}`}
      title={canPet ? `${labelText}（点击摸摸 Kiki）` : labelText}
      className={`relative shrink-0 select-none inline-flex items-center justify-center ${SIZE_CLASSES[size]} ${
        canPet ? 'cursor-pointer active:scale-95 transition-transform' : ''
      } ${className}`}
    >
      {isLarge && (
        <div
          className={`absolute bottom-0.5 left-1/2 -translate-x-1/2 w-[66%] h-2.5 rounded-full bg-[#B8ACA0]/40 blur-[3px] pointer-events-none ${
            effectiveAnimated && resolvedPose === 'delivering' ? 'animate-kiki-shadow' : ''
          }`}
        />
      )}

      {effectiveAnimated && isLarge && resolvedPose === 'delivering' && (
        <div className="absolute -left-1 bottom-6 flex flex-col gap-1 pointer-events-none">
          <span className="w-3.5 h-0.5 rounded-full bg-[#8C8378]/55 animate-kiki-dash" />
          <span className="w-2.5 h-0.5 rounded-full bg-[#8C8378]/40 animate-kiki-dash [animation-delay:220ms]" />
        </div>
      )}
      {effectiveAnimated && isLarge && resolvedPose === 'waiting_input' && (
        <span className="absolute top-0.5 left-1/2 -translate-x-1/2 w-5 h-5 rounded-full bg-amber-400/30 animate-ping pointer-events-none" />
      )}

      <img
        key={`${resolvedPose}-${effectiveAnimated ? 'anim' : 'static'}`}
        src={imgSrc}
        alt={`Kiki ${resolvedPose}`}
        draggable={false}
        onError={() => {
          if (!usePngFallback) setUsePngFallback(true);
        }}
        className={`relative z-10 w-full h-full object-contain drop-shadow-[0_2px_4px_rgba(38,36,34,0.09)] ${motionClass}`}
      />
    </div>
  );

  if (!showLabel) {
    return sprite;
  }

  return (
    <div className="inline-flex items-center gap-2">
      {sprite}
      <span className="text-xs font-medium text-[#5A534C]">{labelText}</span>
    </div>
  );
};

export default KikiAvatar;
