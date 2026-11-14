import type { ReactNode } from "react";
import type { Portal } from "@/lib/router";
import type { PageKey } from "@/pages/registry";

export interface ShellProps {
  page: PageKey;
  onNavigate: (page: PageKey) => void;
  onPortal: (portal: Portal) => void;
  children: ReactNode;
}

export interface NavProps {
  page: PageKey;
  collapsed: boolean;
  onToggle: () => void;
  onNavigate: (page: PageKey) => void;
}

export type IconKey = PageKey | "collapse" | "expand" | "logout" | "shield" | "coin";
