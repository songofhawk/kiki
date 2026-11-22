import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Camera, Check, RotateCcw, User } from 'lucide-react';

export interface UserProfile {
  name: string;
  presetId: string;
  customAvatarDataUrl: string | null;
}

const STORAGE_KEY = 'kiki_customer_profile_v1';
const PROFILE_EVENT = 'kiki:customer-profile-updated';

export interface AvatarPreset {
  id: string;
  label: string;
  bg: string;
  fg: string;
  border: string;
  emoji?: string;
}

export const AVATAR_PRESETS: AvatarPreset[] = [
  {
    id: 'slate',
    label: '雾蓝格调',
    bg: 'bg-[#5B7A95]',
    fg: 'text-white',
    border: 'border-[#4A6882]',
  },
  {
    id: 'warm_ink',
    label: '暖墨极简',
    bg: 'bg-[#34312E]',
    fg: 'text-[#FAF8F5]',
    border: 'border-[#262422]',
  },
  {
    id: 'sage',
    label: '松针青绿',
    bg: 'bg-[#4B7355]',
    fg: 'text-white',
    border: 'border-[#3B6244]',
  },
  {
    id: 'terracotta',
    label: '暖陶杏棕',
    bg: 'bg-[#B06D46]',
    fg: 'text-white',
    border: 'border-[#965835]',
  },
  {
    id: 'coffee',
    label: '手冲咖啡',
    bg: 'bg-[#EFE8DE]',
    fg: 'text-[#4E463E]',
    border: 'border-[#DCD2C4]',
    emoji: '☕',
  },
  {
    id: 'spark',
    label: '创作者',
    bg: 'bg-[#F5EFE2]',
    fg: 'text-[#7A5B25]',
    border: 'border-[#E5D9BE]',
    emoji: '✦',
  },
];

const DEFAULT_PROFILE: UserProfile = {
  name: '我',
  presetId: 'slate',
  customAvatarDataUrl: null,
};

export function loadUserProfile(): UserProfile {
  if (typeof window === 'undefined') return DEFAULT_PROFILE;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_PROFILE;
    const parsed = JSON.parse(raw) as Partial<UserProfile>;
    return {
      name: typeof parsed.name === 'string' && parsed.name.trim() ? parsed.name.trim() : DEFAULT_PROFILE.name,
      presetId: typeof parsed.presetId === 'string' ? parsed.presetId : DEFAULT_PROFILE.presetId,
      customAvatarDataUrl:
        typeof parsed.customAvatarDataUrl === 'string' ? parsed.customAvatarDataUrl : null,
    };
  } catch {
    return DEFAULT_PROFILE;
  }
}

export function saveUserProfile(next: UserProfile): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    window.dispatchEvent(new CustomEvent(PROFILE_EVENT, { detail: next }));
  } catch {
    // Ignore storage quota errors
  }
}

export function useUserProfile(): [UserProfile, (updater: UserProfile | ((prev: UserProfile) => UserProfile)) => void] {
  const [profile, setProfile] = useState<UserProfile>(() => loadUserProfile());

  useEffect(() => {
    const handler = () => setProfile(loadUserProfile());
    window.addEventListener(PROFILE_EVENT, handler);
    window.addEventListener('storage', handler);
    return () => {
      window.removeEventListener(PROFILE_EVENT, handler);
      window.removeEventListener('storage', handler);
    };
  }, []);

  const update = (updater: UserProfile | ((prev: UserProfile) => UserProfile)) => {
    setProfile((prev) => {
      const next = typeof updater === 'function' ? updater(prev) : updater;
      saveUserProfile(next);
      return next;
    });
  };

  return [profile, update];
}

async function resizeImageToDataUrl(file: File, maxDim = 160): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读取图片失败'));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error('解析图片失败'));
      img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = maxDim;
        canvas.height = maxDim;
        const ctx = canvas.getContext('2d');
        if (!ctx) {
          resolve(String(reader.result));
          return;
        }
        // Center-crop square
        const side = Math.min(img.width, img.height);
        const sx = (img.width - side) / 2;
        const sy = (img.height - side) / 2;
        ctx.drawImage(img, sx, sy, side, side, 0, 0, maxDim, maxDim);
        resolve(canvas.toDataURL('image/jpeg', 0.88));
      };
      img.src = String(reader.result);
    };
    reader.readAsDataURL(file);
  });
}

const SIZE_MAP = {
  xs: 'w-5 h-5 text-[10px] rounded-full',
  sm: 'w-8 h-8 text-xs rounded-xl',
  md: 'w-9 h-9 text-xs rounded-2xl',
  lg: 'w-11 h-11 text-sm rounded-2xl',
} as const;

export interface UserAvatarProps {
  size?: keyof typeof SIZE_MAP;
  editable?: boolean;
  className?: string;
}

export const UserAvatar: React.FC<UserAvatarProps> = ({
  size = 'md',
  editable = false,
  className = '',
}) => {
  const [profile, updateProfile] = useUserProfile();
  const [open, setOpen] = useState(false);
  const [draftName, setDraftName] = useState(profile.name);
  const [popoverPos, setPopoverPos] = useState<{ top: number; left: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const preset =
    AVATAR_PRESETS.find((p) => p.id === profile.presetId) ?? AVATAR_PRESETS[0];

  useEffect(() => {
    setDraftName(profile.name);
  }, [profile.name, open]);

  useEffect(() => {
    if (!open || !triggerRef.current) {
      setPopoverPos(null);
      return;
    }
    const updatePos = () => {
      if (!triggerRef.current) return;
      const rect = triggerRef.current.getBoundingClientRect();
      const width = 264;
      const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
      const top = Math.min(rect.bottom + 8, window.innerHeight - 300);
      setPopoverPos({ top, left });
    };
    updatePos();
    window.addEventListener('resize', updatePos);
    window.addEventListener('scroll', updatePos, true);
    return () => {
      window.removeEventListener('resize', updatePos);
      window.removeEventListener('scroll', updatePos, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        !triggerRef.current?.contains(target) &&
        !popoverRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const dataUrl = await resizeImageToDataUrl(file);
      updateProfile((prev) => ({
        ...prev,
        customAvatarDataUrl: dataUrl,
      }));
    } catch {
      // Ignore invalid image
    } finally {
      e.target.value = '';
    }
  };

  const handleSaveName = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = draftName.trim() || '我';
    updateProfile((prev) => ({ ...prev, name: trimmed }));
    setOpen(false);
  };

  const initialChar = Array.from(profile.name.trim())[0] || '';

  const avatarVisual = profile.customAvatarDataUrl ? (
    <img
      src={profile.customAvatarDataUrl}
      alt={profile.name}
      draggable={false}
      className={`${SIZE_MAP[size]} object-cover border border-[#DDD5C9] shadow-sm shrink-0 select-none ${className}`}
    />
  ) : (
    <span
      className={`${SIZE_MAP[size]} ${preset.bg} ${preset.fg} border ${preset.border} inline-flex items-center justify-center font-semibold shadow-sm shrink-0 select-none ${className}`}
    >
      {preset.emoji ? (
        <span>{preset.emoji}</span>
      ) : initialChar && initialChar !== '我' ? (
        <span>{initialChar.toUpperCase()}</span>
      ) : (
        <User className="w-[52%] h-[52%]" />
      )}
    </span>
  );

  if (!editable) {
    return avatarVisual;
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((prev) => !prev);
        }}
        title={`客户资料：${profile.name}（点击自定义头像与昵称）`}
        className="relative group inline-flex items-center justify-center focus:outline-none"
      >
        {avatarVisual}
        <span className="absolute -bottom-0.5 -right-0.5 w-4 h-4 rounded-full bg-white border border-[#DCD4C8] text-[#5A534C] flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity shadow-sm">
          <Camera className="w-2.5 h-2.5" />
        </span>
      </button>

      {open &&
        popoverPos &&
        createPortal(
          <div
            ref={popoverRef}
            style={{ top: popoverPos.top, left: popoverPos.left }}
            onClick={(e) => e.stopPropagation()}
            className="fixed z-[110] w-[264px] rounded-2xl bg-white border border-[#E6DFD5] shadow-xl shadow-stone-900/10 p-3.5 space-y-3 text-left animate-kiki-fade-up"
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-[#262422]">
                客户头像与昵称
              </span>
              {profile.customAvatarDataUrl && (
                <button
                  type="button"
                  onClick={() =>
                    updateProfile((prev) => ({
                      ...prev,
                      customAvatarDataUrl: null,
                    }))
                  }
                  className="inline-flex items-center gap-1 text-[11px] text-[#7D756C] hover:text-[#262422]"
                >
                  <RotateCcw className="w-3 h-3" />
                  恢复预设
                </button>
              )}
            </div>

            {/* Custom Upload + Presets */}
            <div className="flex items-center gap-2 flex-wrap">
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="px-2.5 py-1.5 rounded-xl border border-dashed border-[#C9BFB0] hover:border-[#5B7A95] bg-[#FAF8F5] text-[11px] font-medium text-[#4E4842] inline-flex items-center gap-1.5 transition-colors"
              >
                <Camera className="w-3.5 h-3.5 text-[#5B7A95]" />
                上传头像图片
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                onChange={handleFileChange}
                className="hidden"
              />
            </div>

            <div>
              <div className="text-[10px] font-medium text-[#8C8378] mb-1.5">
                或选择默认风格
              </div>
              <div className="grid grid-cols-6 gap-1.5">
                {AVATAR_PRESETS.map((p) => {
                  const active =
                    !profile.customAvatarDataUrl && profile.presetId === p.id;
                  return (
                    <button
                      key={p.id}
                      type="button"
                      title={p.label}
                      onClick={() =>
                        updateProfile((prev) => ({
                          ...prev,
                          presetId: p.id,
                          customAvatarDataUrl: null,
                        }))
                      }
                      className={`w-8 h-8 rounded-xl border ${p.bg} ${p.fg} ${p.border} flex items-center justify-center text-xs font-semibold transition-transform ${
                        active ? 'ring-2 ring-[#5B7A95] ring-offset-1 scale-105' : 'opacity-85 hover:opacity-100'
                      }`}
                    >
                      {p.emoji ?? <User className="w-3.5 h-3.5" />}
                    </button>
                  );
                })}
              </div>
            </div>

            <form onSubmit={handleSaveName} className="space-y-2 pt-1 border-t border-[#F0EAE1]">
              <label className="block text-[10px] font-medium text-[#8C8378]">
                显示称呼
              </label>
              <div className="flex items-center gap-1.5">
                <input
                  type="text"
                  maxLength={20}
                  value={draftName}
                  onChange={(e) => setDraftName(e.target.value)}
                  placeholder="输入你的昵称..."
                  className="flex-1 min-w-0 px-2.5 py-1.5 text-xs rounded-xl bg-[#FAF8F5] border border-[#E5DED4] focus:outline-none focus:border-[#5B7A95]"
                />
                <button
                  type="submit"
                  className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-xl text-xs font-medium bg-[#5B7A95] text-white hover:bg-[#4A6882] transition-colors shrink-0"
                >
                  <Check className="w-3.5 h-3.5" />
                  保存
                </button>
              </div>
            </form>
          </div>,
          document.body
        )}
    </>
  );
};

export default UserAvatar;
