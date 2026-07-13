const navigationItems = ["Overview", "Portfolio", "Insights", "Learning"];

const foundations = [
  {
    title: "Portfolio",
    description: "A calm home for your future holdings, savings, and cash picture.",
  },
  {
    title: "Insights",
    description: "Evidence-led context will live here once reliable sources are connected.",
  },
  {
    title: "Learning",
    description: "Build a more deliberate investing practice at your own pace.",
  },
];

export default function Home() {
  return (
    <div className="min-h-screen bg-canvas">
      <header className="border-b border-border bg-surface/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4 sm:px-8">
          <a className="text-lg font-semibold tracking-tight text-ink" href="#overview">
            PIA
          </a>
          <span className="rounded-full bg-brand-soft px-3 py-1 text-xs font-semibold tracking-wide text-brand">
            Foundation
          </span>
        </div>
      </header>

      <div className="mx-auto flex max-w-7xl flex-col gap-6 px-5 py-6 sm:px-8 sm:py-10 lg:flex-row lg:gap-10">
        <nav aria-label="Primary navigation" className="lg:w-44 lg:shrink-0">
          <ul className="flex gap-2 overflow-x-auto pb-1 lg:flex-col lg:overflow-visible">
            {navigationItems.map((item) => (
              <li key={item} className="shrink-0">
                <a
                  className={`block rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                    item === "Overview" ? "bg-brand text-white" : "text-ink-muted hover:bg-surface-muted hover:text-ink"
                  }`}
                  href={item === "Overview" ? "#overview" : `#${item.toLowerCase()}`}
                >
                  {item}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <main className="min-w-0 flex-1" id="overview">
          <section className="rounded-panel border border-border bg-surface p-6 shadow-panel sm:p-10">
            <p className="text-sm font-semibold tracking-wide text-brand">PERSONAL INVESTOR ADVISOR</p>
            <h1 className="mt-3 max-w-2xl text-3xl font-semibold tracking-tight text-ink sm:text-5xl">
              Your financial cockpit is taking shape
            </h1>
            <p className="mt-4 max-w-xl text-base leading-7 text-ink-muted sm:text-lg">
              This private workspace is ready for its foundations. Future approved phases will add your data,
              evidence, and decision support—never automatic trading.
            </p>
          </section>

          <section aria-labelledby="foundation-heading" className="mt-6 sm:mt-8">
            <div className="flex items-baseline justify-between gap-4">
              <h2 className="text-xl font-semibold text-ink" id="foundation-heading">
                What&apos;s ahead
              </h2>
              <span className="text-sm text-ink-muted">Static preview</span>
            </div>
            <div className="mt-4 grid gap-4 md:grid-cols-3">
              {foundations.map((foundation) => (
                <article
                  className="rounded-panel border border-border bg-surface p-5 transition-colors hover:border-brand/40"
                  id={foundation.title.toLowerCase()}
                  key={foundation.title}
                >
                  <h2 className="text-lg font-semibold text-ink">{foundation.title}</h2>
                  <p className="mt-2 text-sm leading-6 text-ink-muted">{foundation.description}</p>
                </article>
              ))}
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}
