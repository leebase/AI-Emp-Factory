package httpapi

// The cockpit theme token contract.
//
// A theme here is a set of values for the named custom properties below, and
// nothing else. There is no per-theme markup, no per-theme component class and
// no theme branch in the browser script, so re-theming cannot reach employee
// facts, ordering, derivation or API bytes.
//
// How a stylesheet token override configures the cockpit:
//
//	<style>
//	  :root {
//	    --mc-bg: #fdf3e3;
//	    --mc-surface: #fffaf0;
//	    --mc-ink: #1a1713;
//	    /* ...any subset of the contract below... */
//	  }
//	</style>
//
// Declaring the same property names later in the cascade, at document scope, is
// the whole mechanism. An override never names a component selector. If one
// ever has to, a component is bypassing the contract and that is the defect.
//
// Tokens are declared at document scope on purpose. The employee-detail modal
// is a sibling of #cockpit-view, not a child of it, so tokens scoped to the
// cockpit element could not reach it and the modal was previously written with
// literal colours that no override could touch.
//
// Contract, by responsibility:
//
//	surfaces   --mc-bg --mc-surface --mc-surface-raised --mc-overlay
//	text       --mc-ink --mc-ink-muted
//	borders    --mc-line --mc-line-strong
//	meaning    --mc-accent --mc-good --mc-warn --mc-bad
//	focus      --mc-focus
//	metrics    --mc-radius --mc-gap --mc-target --mc-motion
//
// --mc-line and --mc-line-strong are not interchangeable. --mc-line is
// decorative separation; --mc-line-strong is the boundary of a control whose
// edge is the only thing that says where the control is - the roster search
// field, the nav and filter buttons, the modal and its Close. That boundary
// carries necessary information, so it is held to a computed >= 3:1 against the
// surfaces it is drawn on, which is why it is lighter than a hairline would
// otherwise want to be.
//
// Colour never carries meaning alone: every badge, banner and state also prints
// its words, so a monochrome or colour-blind reading loses nothing and a token
// override cannot remove information.
const cockpitThemeCSS = `
    :root {
      --mc-bg: #100d0b;
      --mc-surface: #1b1613;
      --mc-surface-raised: #271f1a;
      --mc-overlay: rgba(7, 4, 2, 0.82);
      --mc-ink: #f7eee5;
      --mc-ink-muted: #bfae9f;
      --mc-line: #40332b;
      --mc-line-strong: #8b7666;
      --mc-accent: #f2a65a;
      --mc-good: #6fdc9a;
      --mc-warn: #ffc65c;
      --mc-bad: #ff9a8d;
      --mc-focus: #ffd08a;
      --mc-radius: 16px;
      --mc-gap: 8px;
      --mc-target: 44px;
      --mc-motion: 140ms;
    }
`

// cockpitTestThemeOverrideCSS is a TEST-ONLY probe, not a second theme and not
// a product. It ships with no picker, no stored preference, no endpoint and no
// route; it is never concatenated into the served document. Tests inject it
// into the real served page so that the shipped components, not a standalone
// copy of them, are the thing proven to be presentation-independent.
//
// It is deliberately garish and deliberately light-on-dark-inverted so that
// "every visible employee fact is unchanged" is a claim with teeth. It contains
// token declarations only; it names no component selector.
const cockpitTestThemeOverrideCSS = `
    :root {
      --mc-bg: #f6eddc;
      --mc-surface: #fffaf0;
      --mc-surface-raised: #ffe9c4;
      --mc-overlay: rgba(90, 40, 10, 0.55);
      --mc-ink: #1a1207;
      --mc-ink-muted: #4a3617;
      --mc-line: #c8a86a;
      --mc-line-strong: #8a6a24;
      --mc-accent: #7a2f00;
      --mc-good: #1d6b32;
      --mc-warn: #8a5a00;
      --mc-bad: #a01414;
      --mc-focus: #6b1fa8;
      --mc-radius: 0px;
      --mc-gap: 14px;
      --mc-target: 48px;
      --mc-motion: 0ms;
    }
`
