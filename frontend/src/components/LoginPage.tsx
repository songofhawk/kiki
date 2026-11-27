import React, { useState } from 'react';
import { Eye, EyeOff, Loader2, Lock, User } from 'lucide-react';
import { loginWithPassword } from '../api';
import type { AuthStatusResponse } from '../types';
import KikiAvatar from './KikiAvatar';

export interface LoginPageProps {
  onLoginSuccess: (status: AuthStatusResponse) => void;
}

export const LoginPage: React.FC<LoginPageProps> = ({ onLoginSuccess }) => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedUser = username.trim();
    if (!trimmedUser || !password || submitting) return;

    setSubmitting(true);
    setError(null);
    try {
      const status = await loginWithPassword(trimmedUser, password);
      onLoginSuccess(status);
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败，请检查用户名或密码');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen w-screen flex items-center justify-center bg-[#FAF8F5] text-[#262422] p-4 kiki-paper-bg">
      <div className="w-full max-w-sm rounded-3xl bg-white/95 border border-[#E6DFD5] shadow-xl shadow-stone-900/5 p-7 sm:p-8 animate-kiki-fade-up">
        {/* Kiki Brand Header */}
        <div className="flex flex-col items-center text-center">
          <div className="w-20 h-20 rounded-3xl bg-[#F5F1EB] border border-[#E5DED4] shadow-sm flex items-center justify-center overflow-hidden mb-4">
            <KikiAvatar
              status={submitting ? 'thinking' : 'idle'}
              animated
              size="lg"
            />
          </div>
          <h1 className="text-xl font-bold tracking-tight text-[#262422]">
            欢迎回到 Kiki
          </h1>
          <p className="mt-1 text-xs text-[#7D756C]">
            请输入管理员配置的账号密码登录
          </p>
        </div>

        {/* Error Alert */}
        {error && (
          <div
            role="alert"
            className="mt-5 px-3.5 py-2.5 rounded-2xl bg-rose-50 border border-rose-200/80 text-xs text-rose-700 leading-relaxed"
          >
            {error}
          </div>
        )}

        {/* Login Form */}
        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          <div className="space-y-1.5">
            <label
              htmlFor="kiki-login-username"
              className="block text-xs font-medium text-[#5A534C]"
            >
              用户名
            </label>
            <div className="relative">
              <User className="w-4 h-4 text-[#9A9187] absolute left-3.5 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input
                id="kiki-login-username"
                type="text"
                autoComplete="username"
                autoFocus
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                disabled={submitting}
                placeholder="请输入用户名"
                className="w-full pl-10 pr-3.5 py-2.5 text-sm rounded-2xl bg-[#FAF8F5] border border-[#E5DED4] text-[#262422] placeholder:text-[#A89F94] focus:outline-none focus:border-[#5B7A95] focus:bg-white transition-colors disabled:opacity-60"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label
              htmlFor="kiki-login-password"
              className="block text-xs font-medium text-[#5A534C]"
            >
              密码
            </label>
            <div className="relative">
              <Lock className="w-4 h-4 text-[#9A9187] absolute left-3.5 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input
                id="kiki-login-password"
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={submitting}
                placeholder="请输入密码"
                className="w-full pl-10 pr-10 py-2.5 text-sm rounded-2xl bg-[#FAF8F5] border border-[#E5DED4] text-[#262422] placeholder:text-[#A89F94] focus:outline-none focus:border-[#5B7A95] focus:bg-white transition-colors disabled:opacity-60"
              />
              <button
                type="button"
                tabIndex={-1}
                aria-label={showPassword ? '隐藏密码' : '显示密码'}
                onClick={() => setShowPassword((prev) => !prev)}
                className="absolute right-3 top-1/2 -translate-y-1/2 p-1 rounded-lg text-[#8C8378] hover:text-[#262422] transition-colors"
              >
                {showPassword ? (
                  <EyeOff className="w-4 h-4" />
                ) : (
                  <Eye className="w-4 h-4" />
                )}
              </button>
            </div>
          </div>

          <button
            type="submit"
            disabled={submitting || !username.trim() || !password}
            className="w-full mt-2 py-2.5 px-4 rounded-2xl text-sm font-semibold text-white bg-[#5B7A95] hover:bg-[#4A6882] active:scale-[0.99] shadow-sm transition-all inline-flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting && <Loader2 className="w-4 h-4 animate-spin" />}
            <span>{submitting ? '正在验证...' : '登录'}</span>
          </button>
        </form>

        <p className="mt-6 text-center text-[11px] text-[#9A9187]">
          个人私有部署 · 暂不开放自助注册
        </p>
      </div>
    </div>
  );
};

export default LoginPage;
