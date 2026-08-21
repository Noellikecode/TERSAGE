import type { Config } from 'tailwindcss';

/**
 * Dark, flat, high contrast. No gradients, no glow.
 *
 * The three assertion states each get a colour AND are always paired with a
 * label or shape in the markup, so they survive colourblindness and a daylight
 * tablet.
 */
const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ground: '#0a0c0f',
        surface: '#12161c',
        raised: '#1a2029',
        line: '#2a323d',
        ink: '#e8edf4',
        muted: '#8b97a8',
        confirmed: '#4ade80',
        disputed: '#fbbf24',
        unknown: '#7c8b9a',
        alarm: '#f87171',
        live: '#38bdf8',
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      fontSize: {
        micro: ['0.6875rem', { lineHeight: '1rem' }],
      },
    },
  },
  plugins: [],
};

export default config;
