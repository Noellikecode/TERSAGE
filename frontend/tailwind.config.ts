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
        // A scale, added because there wasn't one. The console was 183 uses of
        // `micro` against 8 of anything larger, so every element on screen
        // spoke at the same volume and nothing could be scanned -- an admin had
        // to read the page to understand it. These are for the district bar,
        // the survey queue and the panels around them; the agent cards keep
        // their own sizing deliberately.
        //
        // `hero` is a number somebody should be able to read across a room.
        hero: ['2.75rem', { lineHeight: '1', letterSpacing: '-0.02em' }],
        // A heading that names a region of the page, once.
        title: ['1.0625rem', { lineHeight: '1.4' }],
        // The default for anything that is actually read rather than glanced.
        body: ['0.875rem', { lineHeight: '1.5' }],
        // A label above a number. Small on purpose -- it is the number's
        // caption, and the number is the message.
        label: ['0.75rem', { lineHeight: '1rem', letterSpacing: '0.08em' }],
      },
    },
  },
  plugins: [],
};

export default config;
