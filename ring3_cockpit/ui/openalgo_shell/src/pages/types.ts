export type WorkspaceMode = "PAPER" | "LIVE";

export interface PageProps {
  mode: WorkspaceMode;
}

export interface ApiNote {
  tone: "neutral" | "bull" | "bear";
  message: string;
}
