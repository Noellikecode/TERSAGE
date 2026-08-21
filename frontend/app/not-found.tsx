export default function NotFound() {
  return (
    <main className="flex min-h-screen items-center justify-center p-8">
      <div className="border border-line bg-surface p-6">
        <h1 className="text-base text-ink">Not found</h1>
        <p className="mt-2 text-micro text-muted">
          No such view. The command center lives at <code className="text-ink">/</code>.
        </p>
      </div>
    </main>
  );
}
