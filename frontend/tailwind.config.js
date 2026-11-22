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
          '0%, 100%': { transform: 'scale(1) translateY(0px)' },
          '50%': { transform: 'scale(1.025) translateY(-1px)' },
        },
        kikiThink: {
          '0%, 100%': { transform: 'rotate(-2deg) translateY(0px)' },
          '50%': { transform: 'rotate(2.5deg) translateY(-2px)' },
        },
        kikiRun: {
          '0%, 100%': { transform: 'translateY(0px) rotate(-1.5deg)' },
          '50%': { transform: 'translateY(-3px) rotate(1.5deg)' },
        },
        kikiWave: {
          '0%, 100%': { transform: 'translateY(0px) scale(1)' },
          '30%': { transform: 'translateY(-2.5px) scale(1.04)' },
          '60%': { transform: 'translateY(0px) scale(1)' },
        },
        toastSlideIn: {
          '0%': { opacity: '0', transform: 'translateX(24px)' },
          '100%': { opacity: '1', transform: 'translateX(0)' },
        },
      },
      animation: {
        'kiki-breathe': 'kikiBreathe 3.6s ease-in-out infinite',
        'kiki-think': 'kikiThink 2.2s ease-in-out infinite',
        'kiki-run': 'kikiRun 0.55s ease-in-out infinite',
        'kiki-wave': 'kikiWave 1.6s ease-in-out infinite',
        'toast-in': 'toastSlideIn 0.25s ease-out forwards',
      },
    },
  },
  plugins: [],
}
