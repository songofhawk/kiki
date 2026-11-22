/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Microsoft YaHei"',
          'Inter',
          'system-ui',
          'sans-serif',
        ],
        mono: ['"JetBrains Mono"', '"SF Mono"', 'Menlo', 'monospace'],
      },
      keyframes: {
        kikiBreathe: {
          '0%, 100%': { transform: 'translateY(0px) scaleY(1) scaleX(1)' },
          '50%': { transform: 'translateY(-1.5px) scaleY(1.018) scaleX(0.992)' },
        },
        kikiThink: {
          '0%, 100%': { transform: 'translateY(0px)' },
          '50%': { transform: 'translateY(-2px)' },
        },
        kikiRun: {
          '0%, 100%': { transform: 'translateY(0px) translateX(0px)' },
          '50%': { transform: 'translateY(-2.5px) translateX(2px)' },
        },
        kikiWave: {
          '0%, 100%': { transform: 'translateY(0px)' },
          '50%': { transform: 'translateY(-2px)' },
        },
        kikiShadow: {
          '0%, 100%': { transform: 'translateX(-50%) scaleX(1)', opacity: '0.35' },
          '50%': { transform: 'translateX(-50%) scaleX(0.86)', opacity: '0.2' },
        },
        kikiDash: {
          '0%': { transform: 'translateX(6px)', opacity: '0' },
          '40%': { opacity: '0.8' },
          '100%': { transform: 'translateX(-10px)', opacity: '0' },
        },
        kikiFadeIn: {
          '0%': { opacity: '0', transform: 'scale(0.97)' },
          '100%': { opacity: '1', transform: 'scale(1)' },
        },
        kikiFadeOut: {
          '0%': { opacity: '1', transform: 'scale(1)' },
          '100%': { opacity: '0', transform: 'scale(1)' },
        },
        toastSlideIn: {
          '0%': { opacity: '0', transform: 'translateX(24px)' },
          '100%': { opacity: '1', transform: 'translateX(0)' },
        },
      },
      animation: {
        'kiki-breathe': 'kikiBreathe 3.6s ease-in-out infinite',
        'kiki-think': 'kikiThink 2.4s ease-in-out infinite',
        'kiki-run': 'kikiRun 0.48s ease-in-out infinite',
        'kiki-wave': 'kikiWave 1.4s ease-in-out infinite',
        'kiki-shadow': 'kikiShadow 0.48s ease-in-out infinite',
        'kiki-dash': 'kikiDash 0.65s linear infinite',
        'kiki-fade-in': 'kikiFadeIn 220ms ease-in-out both',
        'kiki-fade-out': 'kikiFadeOut 220ms ease-in-out both',
        'toast-in': 'toastSlideIn 0.25s ease-out forwards',
      },
    },
  },
  plugins: [],
}
