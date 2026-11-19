/**
 * Minimal history-API router for the 56-route terminal.
 *
 * The FastAPI gateway serves `dist/index.html` for every client path
 * (`cockpit_spa_fallback`), so routing is purely client side and must survive a
 * hard refresh on `/live/terminal` exactly like a click from `/dashboard`.
 */

import { type ReactNode, createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

interface RouterValue {
  location: string;
  pathname: string;
  search: string;
  navigate: (to: string, options?: { replace?: boolean }) => void;
}

const RouterContext = createContext<RouterValue | null>(null);

function readLocation(): string {
  if (typeof window === "undefined") return "/";
  return `${window.location.pathname}${window.location.search}`;
}

export function RouterProvider({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState<string>(readLocation);

  useEffect(() => {
    const onPop = () => setLocation(readLocation());
    window.addEventListener("popstate", onPop);
    window.addEventListener("nios:navigate", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      window.removeEventListener("nios:navigate", onPop);
    };
  }, []);

  const navigate = useCallback((to: string, options?: { replace?: boolean }) => {
    const target = to.length > 0 ? to : "/";
    if (target === readLocation()) {
      setLocation(target);
      return;
    }
    if (options?.replace) window.history.replaceState({}, "", target);
    else window.history.pushState({}, "", target);
    setLocation(target);
    window.scrollTo({ top: 0 });
  }, []);

  const value = useMemo<RouterValue>(() => {
    const index = location.indexOf("?");
    return {
      location,
      pathname: index === -1 ? location : location.slice(0, index),
      search: index === -1 ? "" : location.slice(index),
      navigate,
    };
  }, [location, navigate]);

  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

export function useRouter(): RouterValue {
  const context = useContext(RouterContext);
  if (!context) throw new Error("useRouter must be used inside <RouterProvider>");
  return context;
}

export function usePathname(): string {
  return useRouter().pathname;
}

export function useSearchParams(): URLSearchParams {
  const { search } = useRouter();
  return useMemo(() => new URLSearchParams(search), [search]);
}

export function useQueryParam(name: string): string | null {
  const params = useSearchParams();
  return params.get(name);
}

/** Imperative navigation usable outside React (e.g. post-login redirect). */
export function navigateTo(to: string, options?: { replace?: boolean }): void {
  if (typeof window === "undefined") return;
  if (options?.replace) window.history.replaceState({}, "", to);
  else window.history.pushState({}, "", to);
  window.dispatchEvent(new Event("nios:navigate"));
  window.scrollTo({ top: 0 });
}

interface LinkProps {
  to: string;
  children: ReactNode;
  className?: string;
  activeClassName?: string;
  title?: string;
  onClick?: () => void;
}

export function Link({ to, children, className, activeClassName, title, onClick }: LinkProps) {
  const { pathname, navigate } = useRouter();
  const active = pathname === to || (to !== "/" && pathname.startsWith(`${to}/`));
  const classes = [className, active && activeClassName ? activeClassName : ""].filter(Boolean).join(" ");
  return (
    <a
      href={to}
      title={title}
      className={classes}
      aria-current={active ? "page" : undefined}
      onClick={(event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
        event.preventDefault();
        onClick?.();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}
