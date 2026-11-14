/**
 * Minimal, dependency-free SPA router.
 *
 * The gateway already serves `dist/index.html` for every non-API path, so the
 * cockpit only needs to translate `location.pathname` into a route and push
 * history on navigation. Portals are matched before the workspace prefixes so
 * `/paper/login` can never be mistaken for the paper `login` page.
 */

import { useCallback, useEffect, useState } from "react";
import { pageFromSegment, type PageKey } from "@/pages/registry";
import type { Workspace } from "./types";

export type Portal = "setup" | "login" | "paper-login" | "live-login" | "live-unlock";

export interface RouteLocation {
  path: string;
  search: string;
  portal: Portal | null;
  workspace: Workspace;
  page: PageKey;
  /** True when the path is neither a portal nor a known page. */
  unknown: boolean;
}

const NAV_EVENT = "nios:navigate";

const PORTAL_PATHS: Record<string, Portal> = {
  "/setup": "setup",
  "/login": "login",
  "/paper/login": "paper-login",
  "/live/login": "live-login",
  "/live-unlock": "live-unlock",
};

export function parsePath(pathname: string, search = ""): RouteLocation {
  const clean = pathname.replace(/\/+$/, "") || "/";

  const portal = PORTAL_PATHS[clean];
  if (portal) {
    return {
      path: clean,
      search,
      portal,
      workspace: portal === "live-login" || portal === "live-unlock" ? "live" : "base",
      page: "dashboard",
      unknown: false,
    };
  }

  if (clean === "/paper") {
    return { path: clean, search, portal: null, workspace: "paper", page: "dashboard", unknown: false };
  }
  if (clean === "/live") {
    return { path: clean, search, portal: null, workspace: "live", page: "dashboard", unknown: false };
  }
  if (clean.startsWith("/paper/")) {
    const page = pageFromSegment(clean.slice("/paper/".length));
    return { path: clean, search, portal: null, workspace: "paper", page: page ?? "dashboard", unknown: page === null };
  }
  if (clean.startsWith("/live/")) {
    const page = pageFromSegment(clean.slice("/live/".length));
    return { path: clean, search, portal: null, workspace: "live", page: page ?? "dashboard", unknown: page === null };
  }

  const page = pageFromSegment(clean);
  return { path: clean, search, portal: null, workspace: "base", page: page ?? "dashboard", unknown: page === null };
}

export function currentLocation(): RouteLocation {
  return parsePath(window.location.pathname, window.location.search);
}

export function navigate(path: string, options: { replace?: boolean } = {}): void {
  const target = path.startsWith("/") ? path : `/${path}`;
  const here = `${window.location.pathname}${window.location.search}`;
  if (target === here) {
    window.dispatchEvent(new Event(NAV_EVENT));
    return;
  }
  if (options.replace) window.history.replaceState({}, "", target);
  else window.history.pushState({}, "", target);
  window.dispatchEvent(new Event(NAV_EVENT));
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
}

/** Build the canonical path for one page inside a workspace. */
export function pagePath(page: PageKey, workspace: Workspace = "base"): string {
  return workspace === "base" ? `/${page}` : `/${workspace}/${page}`;
}

export function portalPath(portal: Portal): string {
  return Object.keys(PORTAL_PATHS).find((path) => PORTAL_PATHS[path] === portal) ?? "/login";
}

export function useLocation(): RouteLocation {
  const [location, setLocation] = useState<RouteLocation>(() => currentLocation());

  useEffect(() => {
    const sync = () => setLocation(currentLocation());
    window.addEventListener("popstate", sync);
    window.addEventListener(NAV_EVENT, sync);
    return () => {
      window.removeEventListener("popstate", sync);
      window.removeEventListener(NAV_EVENT, sync);
    };
  }, []);

  return location;
}

export function useNavigate(): (path: string, options?: { replace?: boolean }) => void {
  return useCallback((path: string, options?: { replace?: boolean }) => navigate(path, options), []);
}
