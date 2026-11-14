/**
 * Canonical module for the PAPER SANDBOX shell.
 *
 * The implementation lives in `pages/workspace.tsx` next to the LIVE shell so
 * the two ribbons are always read together; this module gives the shell its
 * own import path for pages and tests that want to speak about one shell.
 */
export { PaperShell, PAPER_ROUTES } from '@/pages/workspace'
