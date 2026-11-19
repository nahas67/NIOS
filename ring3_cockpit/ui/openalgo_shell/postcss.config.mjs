import tailwind from "@tailwindcss/postcss";
import flatness from "./postcss.flatness.mjs";

/**
 * Tailwind first (emits the utility layer), then the NIOS flatness scrubber so
 * any elevation, blur or gradient declaration is deterministically removed
 * before the CSS asset is written.
 */
export default {
  plugins: [tailwind(), flatness],
};
