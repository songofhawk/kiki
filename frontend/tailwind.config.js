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
          '45%': { transform: 'translateY(-2px) scaleY(1.03) scaleX(0.99)' },
          '75%': { transform: 'translateY(0.5px) scaleY(0.99) scaleX(1.01)' },
        },
        kikiThink: {
          '0%, 100%': { transform: 'rotate(-3deg) translateY(0px)' },
          '50%': { transform: 'rotate(3.5deg) translateY(-4px)' },
        },
        kikiRun: {
          '0%, 100%': { transform: 'translateY(0px) translateX(0px) rotate(-2deg)' },
          '25%': { transform: 'translateY(-5px) translateX(2px) rotate(1.5deg)' },
          '50%': { transform: 'translateY(-1px) translateX(3px) rotate(2.5deg)' },
          '75%': { transform: 'translateY(-4px) translateX(1px) rotate(-1deg)' },
        },
        kikiWave: {
          '0%, 100%': { transform: 'translateY(0px) scale(1)' },
          '30%': { transform: 'translateY(-5px) scale(1.04) rotate(-1.5deg)' },
          '60%': { transform: 'translateY(-1px) scale(1.01) rotate(1deg)' },
        },
        kikiShadow: {
          '0%, 100%': { transform: 'scaleX(1)', opacity: '0.32' },
          '50%': { transform: 'scaleX(0.88)', opacity: '0.2' },
        },
        kikiDash: {
          '0%': { transform: 'translateX(6px)', opacity: '0' },
          '40%': { opacity: '0.8' },
          '100%': { transform: 'translateX(-10px)', opacity: '0' },
        },
        toastSlideIn: {
          '0%': { opacity: '0', transform: 'translateX(24px)' },
          '100%': { opacity: '1', transform: 'translateX(0)' },
        },
      },
      animation: {
        'kiki-breathe': 'kikiBreathe 3.4s ease-in-out infinite',
        'kiki-think': 'kikiThink 2.2s ease-in-out infinite',
        'kiki-run': 'kikiRun 0.65s ease-in-out infinite',
        'kiki-wave': 'kikiWave 1.5s ease-in-out infinite',
        'kiki-shadow': 'kikiShadow 3.4s ease-in-out infinite',
        'kiki-dash': 'kikiDash 0.8s linear infinite',
        'toast-in': 'toastSlideIn 0.25s ease-out forwards',
      },
    },
  },
  plugins: [],
}
