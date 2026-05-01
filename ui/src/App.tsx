import { NavLink, Outlet, Route, Routes } from "react-router-dom";
import { Activity, Database, DownloadCloud, Moon, Search, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import Dashboard from "./views/Dashboard";
import Catalog from "./views/Catalog";
import SearchView from "./views/Search";
import Downloads from "./views/Downloads";

const nav = [
  { to: "/", label: "Dashboard", icon: Activity },
  { to: "/catalog", label: "Catalog", icon: Database },
  { to: "/search", label: "Search", icon: Search },
  { to: "/downloads", label: "Downloads", icon: DownloadCloud }
];

function Layout() {
  const [dark, setDark] = useState(() => {
    const stored = window.localStorage.getItem("mnemosyne-theme");
    if (stored === "dark") return true;
    if (stored === "light") return false;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
  });

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    window.localStorage.setItem("mnemosyne-theme", dark ? "dark" : "light");
  }, [dark]);

  return (
    <div className="min-h-screen bg-paper text-ink">
      <header className="border-b border-line bg-white">
        <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-3 md:flex-row md:items-center md:justify-between">
          <div>
            <div className="text-lg font-semibold">Mnemosyne Inference</div>
            <div className="text-xs uppercase tracking-wide text-stone-500">Admin Plane</div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <nav className="flex flex-wrap gap-1">
              {nav.map(({ to, label, icon: Icon }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={to === "/"}
                  className={({ isActive }) =>
                    `focus-ring inline-flex items-center gap-2 border px-3 py-1.5 text-sm ${
                      isActive ? "border-pine bg-pine text-white" : "border-line bg-white text-ink hover:bg-stone-100"
                    }`
                  }
                >
                  <Icon className="h-4 w-4" aria-hidden />
                  {label}
                </NavLink>
              ))}
            </nav>
            <button
              className="focus-ring inline-flex h-9 w-9 items-center justify-center border border-line bg-white hover:bg-stone-100"
              type="button"
              onClick={() => setDark((value) => !value)}
              title={dark ? "Use light mode" : "Use dark mode"}
              aria-label={dark ? "Use light mode" : "Use dark mode"}
            >
              {dark ? <Sun className="h-4 w-4" aria-hidden /> : <Moon className="h-4 w-4" aria-hidden />}
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-5">
        <Outlet />
      </main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="catalog" element={<Catalog />} />
        <Route path="search" element={<SearchView />} />
        <Route path="downloads" element={<Downloads />} />
      </Route>
    </Routes>
  );
}
