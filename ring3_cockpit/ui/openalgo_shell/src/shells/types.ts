import type { ReactNode } from "react";
import type { PageKey } from "@/pages/registry";
import type { Portal } from "@/lib/router";

export interface ShellProps {
  page: PageKey;
  children: ReactNode;
  onNavigate: (page: PageKey) => void;
  onPortal: (portal: Portal) => void;
}
