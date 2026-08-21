import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'FIRST DUE — Command Center',
  description:
    'Municipal structural intelligence. Decision-support prototype; not a certified public-safety system.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen font-mono text-sm">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:bg-raised focus:px-3 focus:py-2"
        >
          Skip to main content
        </a>
        {children}
      </body>
    </html>
  );
}
