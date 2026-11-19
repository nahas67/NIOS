/**
 * NIOS flat-matte enforcement plugin.
 *
 * The institutional terminal is deliberately flat: depth comes from luminance
 * shifts between charcoal layers and 1px hairline borders, never from
 * elevation, blur or gradients.  This plugin removes any such declaration that
 * a utility class, a preflight rule or a future dependency tries to emit.
 */

const FORBIDDEN_PROPS = new Set([
  "box-shadow",
  "-webkit-box-shadow",
  "text-shadow",
  "backdrop-filter",
  "-webkit-backdrop-filter",
  "filter",
]);

const GRADIENT_RE = /(?:linear|radial|conic|repeating-linear|repeating-radial)-gradient\s*\(/i;
const PILL_RE = /(?:^|\s)(?:9999px|99999px|999px)(?:\s|$)/;
const RADIUS_PROP_RE = /border(?:-[a-z]+)*-radius$/i;

/** @type {import('postcss').Plugin} */
const flatness = {
  postcssPlugin: "nios-flatness",
  OnceExit(root) {
    root.walkDecls((decl) => {
      const prop = decl.prop.toLowerCase();
      const value = decl.value ?? "";

      if (FORBIDDEN_PROPS.has(prop)) {
        // `filter` is only forbidden when it blurs; opacity/grayscale are flat.
        if (prop !== "filter" || /blur\s*\(/i.test(value)) {
          decl.remove();
          return;
        }
      }

      if (prop === "transition-property" || prop === "will-change") {
        // A transition must not even advertise a property that cannot exist.
        const parts = value.split(",");
        const kept = parts.filter((part) => !FORBIDDEN_PROPS.has(part.trim().toLowerCase()));
        if (kept.length !== parts.length) {
          decl.value = kept.length ? kept.join(",") : "none";
        }
        return;
      }

      if (GRADIENT_RE.test(value)) {
        decl.remove();
        return;
      }

      if (RADIUS_PROP_RE.test(prop) && PILL_RE.test(` ${value} `)) {
        decl.value = "8px";
        return;
      }

      if (prop === "background-image" && /url\(\s*["']?data:image\/svg/i.test(value)) {
        // Inline SVG data URIs can smuggle gradient fills; drop them outright.
        if (/%3C(?:linear|radial)Gradient/i.test(value)) decl.remove();
      }
    });
  },
};

export default flatness;
