package httpapi

// cockpitLayoutCSS is the composition layer: the roster grid and its cards, the
// sibling employee-detail modal, and the responsive / user-preference rules.
//
// Compositions this file is responsible for:
//
//   - Desktop is an auto-filling card grid; a 390px iPhone viewport is a single
//     column, and so are the per-card metrics.
//   - Long identifiers, URIs and detail text wrap rather than forcing the
//     document into horizontal scroll, at any width and at 200% text scale.
//   - Interactive targets are at least --mc-target (44px) in both directions.
//   - Nothing is revealed on hover. Hover only tints a surface.
//   - The modal reads the same tokens as the roster. It is a sibling of
//     #cockpit-view, so this is only true because the tokens live at document
//     scope; nothing here may reintroduce a literal colour.
const cockpitLayoutCSS = `
    .mc-gate, .mc-burn {
      display: block;
      width: 100%;
      text-align: left;
      padding: 12px 14px;
      margin-bottom: var(--mc-gap);
      min-height: var(--mc-target);
      border: 1px solid var(--mc-line);
      border-left-width: 6px;
      border-radius: var(--mc-radius);
      background: var(--mc-surface);
      color: var(--mc-ink);
      font: inherit;
      cursor: pointer;
      overflow-wrap: anywhere;
    }

    .mc-gate { border-left-color: var(--mc-accent); }
    .mc-burn { border-left-color: var(--mc-bad); }

    .mc-gate:hover, .mc-burn:hover, .mc-card:hover { background: var(--mc-surface-raised); }

    /* min() keeps the track from demanding 280px on a narrow viewport, which is
       what turns a long employee id into a horizontally scrolling page. */
    .mc-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr));
      gap: 12px;
    }

    .mc-card {
      display: block;
      width: 100%;
      min-width: 0;
      text-align: left;
      padding: 14px;
      min-height: var(--mc-target);
      border: 1px solid var(--mc-line);
      border-radius: var(--mc-radius);
      background: var(--mc-surface);
      color: var(--mc-ink);
      font: inherit;
      cursor: pointer;
      overflow-wrap: anywhere;
      transition: background var(--mc-motion) linear;
    }

    .mc-card-name { font-size: 1rem; font-weight: 700; margin-bottom: 6px; }

    .mc-badges { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: var(--mc-gap); }

    .mc-badge {
      padding: 4px 8px;
      border-radius: 999px;
      border: 1px solid var(--mc-line-strong);
      font-size: 0.75rem;
      font-weight: 700;
      background: var(--mc-surface-raised);
      color: var(--mc-ink);
    }

    .mc-badge[data-tone="bad"] { border-color: var(--mc-bad); }
    .mc-badge[data-tone="warn"] { border-color: var(--mc-warn); }
    .mc-badge[data-tone="good"] { border-color: var(--mc-good); }

    .mc-reason { color: var(--mc-ink-muted); font-size: 0.8125rem; margin-bottom: 10px; }

    .mc-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(120px, 100%), 1fr)); gap: var(--mc-gap); }

    .mc-metric {
      min-width: 0;
      padding: var(--mc-gap);
      border-radius: var(--mc-radius);
      background: var(--mc-surface-raised);
    }

    .mc-metric-label {
      display: block;
      font-size: 0.6875rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--mc-ink-muted);
    }

    .mc-metric-value { display: block; font-size: 0.8125rem; font-weight: 600; overflow-wrap: anywhere; }

    .mc-foot {
      margin-top: 10px;
      padding-top: var(--mc-gap);
      border-top: 1px solid var(--mc-line);
      font-size: 0.75rem;
      color: var(--mc-ink-muted);
    }

    .mc-detail-backdrop {
      position: fixed;
      inset: 0;
      background: var(--mc-overlay);
      display: flex;
      align-items: flex-start;
      justify-content: center;
      padding: 16px;
      padding-top: max(16px, env(safe-area-inset-top));
      padding-left: max(16px, env(safe-area-inset-left));
      padding-right: max(16px, env(safe-area-inset-right));
      padding-bottom: max(16px, env(safe-area-inset-bottom));
      overflow: auto;
      z-index: 40;
    }

    /* A display rule outranks the hidden attribute, so the closed backdrop has
       to be taken out of the layout explicitly. Without this it stays a
       full-viewport overlay that silently swallows every click and tap. */
    .mc-detail-backdrop[hidden] { display: none; }

    .mc-detail {
      background: var(--mc-surface);
      color: var(--mc-ink);
      border: 1px solid var(--mc-line-strong);
      border-radius: var(--mc-radius);
      padding: 16px;
      width: min(760px, 100%);
      overflow-wrap: anywhere;
    }

    .mc-detail h3 { font-size: 1.125rem; }
    .mc-detail dt { font-weight: 700; margin-top: 10px; font-size: 0.8125rem; }
    .mc-detail dd { margin: 2px 0 0; color: var(--mc-ink-muted); font-size: 0.8125rem; }
    .mc-detail a { color: var(--mc-accent); }

    /* The staff brief. Four answers, each its own block, sized so the answer
       itself is the largest thing in the block and the source qualifier is
       visibly subordinate to it rather than competing with it. */
    .mc-answers { display: grid; gap: 10px; margin-top: 12px; }

    .mc-answer {
      border: 1px solid var(--mc-line);
      border-radius: var(--mc-radius);
      background: var(--mc-surface-raised);
      padding: 10px 12px;
    }

    .mc-answer-q { margin: 0; font-size: 0.8125rem; font-weight: 700; color: var(--mc-ink-muted); }
    .mc-answer-a { margin: 4px 0 0; font-size: 0.9375rem; color: var(--mc-ink); }
    .mc-answer-note { margin: 6px 0 0; font-size: 0.8125rem; color: var(--mc-ink); }
    .mc-answer-src { margin: 6px 0 0; font-size: 0.6875rem; color: var(--mc-ink-muted); }

    /* The disclosure holding every record, id, reason and vintage. Its summary
       is a real tap target at the touch floor, because on a phone it is the
       one control between the brief and everything under it. */
    .mc-evidence { margin-top: 16px; border-top: 1px solid var(--mc-line); }

    .mc-evidence-summary {
      display: flex;
      align-items: center;
      min-height: var(--mc-target);
      padding: 10px 2px;
      font-size: 0.875rem;
      font-weight: 700;
      color: var(--mc-ink);
      cursor: pointer;
    }

    .mc-detail-close {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: var(--mc-target);
      min-width: var(--mc-target);
      padding: 10px 14px;
      border-radius: var(--mc-radius);
      border: 1px solid var(--mc-line-strong);
      background: var(--mc-surface-raised);
      color: var(--mc-ink);
      font: inherit;
      text-decoration: none;
      cursor: pointer;
    }

    .mc-secondary { margin-top: 14px; font-size: 0.75rem; color: var(--mc-ink-muted); }

    /* History is a long, dense, read-only list. It gets its own scroll bound so
       a hundred records cannot push the rest of the dialog out of reach, and
       every record keeps a visible edge so two records are never read as one.
       overflow-wrap matters here: an opaque evidence identifier is one long
       unbreakable token and must wrap rather than widen a 320px phone. */
    .mc-history { max-height: 60vh; overflow-y: auto; overflow-wrap: anywhere; }
    .mc-history-owner {
      margin-top: 12px;
      padding-top: 8px;
      border-top: 1px solid var(--mc-line-strong);
    }
    .mc-history-owner h4 { margin: 0 0 6px; font-size: 0.9375rem; }
    .mc-history-group {
      margin: 8px 0 4px;
      font-size: 0.8125rem;
      color: var(--mc-ink-muted);
    }
    .mc-history-record {
      margin: 0 0 8px;
      padding: 8px 10px;
      border: 1px solid var(--mc-line);
      border-radius: var(--mc-radius);
      background: var(--mc-surface-raised);
    }
    .mc-history-record p { margin: 0 0 4px; }
    .mc-history-record p:last-child { margin-bottom: 0; }

    @media (max-width: 430px) {
      .mc-grid { grid-template-columns: 1fr; }
      .mc-metrics { grid-template-columns: 1fr; }
      /* On a narrow phone the dialog is already the whole screen; a second
         nested scroll region there traps the reader, so history scrolls with
         the dialog instead. */
      .mc-history { max-height: none; overflow-y: visible; }
      /* Longhand on purpose. A padding shorthand here silently overwrites the
         safe-area longhands declared earlier in the cascade, which is exactly
         where a notch or a home indicator starts clipping content. */
      #cockpit-view, #staff-view {
        padding-top: 12px;
        padding-left: max(10px, env(safe-area-inset-left));
        padding-right: max(10px, env(safe-area-inset-right));
        padding-bottom: max(24px, env(safe-area-inset-bottom));
        border-radius: 0;
      }
    }

    /* Staff is a people list, not a status wall. Every card is one real button
       with one fixed rem-sized block. Recorded mechanics live only on the
       person page so an employee cannot be duplicated by a run or ask. */
    .mc-staff-top { margin-bottom: 16px; }
    .mc-staff-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-width: 0;
    }
    #staff-view .mc-staff-heading h2 { font-size: 1.125rem; }
    .mc-staff-heading button,
    .mc-staff-about > summary,
    .mc-staff-modes button,
    .mc-staff-back { min-height: var(--mc-target); }
    .mc-staff-heading button,
    .mc-staff-modes button,
    .mc-staff-back {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 10px 14px;
      border: 1px solid var(--mc-line-strong);
      border-radius: var(--mc-radius);
      background: var(--mc-surface-raised);
      color: var(--mc-ink);
      font: inherit;
      cursor: pointer;
    }
    .mc-staff-status { margin-top: 8px; color: var(--mc-ink-muted); }
    .mc-staff-about { margin-top: 8px; min-width: 0; }
    .mc-staff-about > summary {
      display: flex;
      align-items: center;
      padding: 8px 2px;
      color: var(--mc-ink-muted);
      cursor: pointer;
    }
    .mc-staff-about > div, .mc-staff-about > p { margin: 8px 0 0; color: var(--mc-ink-muted); overflow-wrap: anywhere; }
    .mc-staff-about-meta { font-size: 0.75rem; }
    .mc-staff-filters { display: flex; flex-wrap: wrap; gap: var(--mc-gap); margin-top: 12px; min-width: 0; }
    .mc-staff-filters button { min-height: var(--mc-target); padding: 8px 12px; border: 1px solid var(--mc-line-strong); border-radius: var(--mc-radius); background: var(--mc-surface-raised); color: var(--mc-ink); font: inherit; cursor: pointer; }
    .mc-staff-filters button[aria-pressed="true"] { border-color: var(--mc-accent); font-weight: 700; }
    .mc-staff-modes { display: flex; flex-wrap: wrap; gap: var(--mc-gap); margin-top: 12px; }

    #staff-roster { display: grid; grid-template-columns: minmax(0, 1fr); min-width: 0; }
    .mc-staff-card {
      display: grid;
      grid-template-columns: 48px minmax(0, 1fr);
      gap: 12px;
      width: 100%;
      height: 8.25rem;
      min-height: 8.25rem;
      max-height: 8.25rem;
      margin: 0 0 var(--mc-gap);
      padding: 12px;
      border: 1px solid var(--mc-line);
      border-radius: var(--mc-radius);
      background: var(--mc-surface);
      color: var(--mc-ink);
      font: inherit;
      text-align: left;
      cursor: pointer;
      overflow: hidden;
      transition: background var(--mc-motion) linear;
    }
    .mc-staff-card:hover { background: var(--mc-surface-raised); }
    .mc-staff-face, .mc-staff-person-face {
      display: flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
      width: 48px;
      height: 48px;
      border-radius: 50%;
      background: var(--mc-surface-raised);
      color: var(--mc-ink);
      font-size: 1.125rem;
      font-weight: 800;
    }
    .mc-staff-copy { display: grid; min-width: 0; align-content: start; gap: 2px; }
    .mc-staff-name, .mc-staff-role, .mc-staff-work, .mc-staff-nudge {
      display: block;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .mc-staff-name { font-size: 1.125rem; line-height: 1.2; font-weight: 700; }
    .mc-staff-role { font-size: 0.8125rem; line-height: 1.35; color: var(--mc-ink-muted); }
    .mc-staff-work { font-size: 0.875rem; line-height: 1.4; font-weight: 600; }
    .mc-staff-missing { color: var(--mc-ink-muted); font-weight: 400; }
    .mc-staff-nudge { display: flex; align-items: center; gap: 6px; min-height: 20px; color: var(--mc-accent); font-size: 0.875rem; line-height: 1.35; }
    .mc-staff-nudge-dot { width: 8px; height: 8px; flex: 0 0 8px; border-radius: 50%; background: var(--mc-accent); }

    .mc-staff-person { min-width: 0; }
    #staff-view .mc-staff-person-header {
      display: grid;
      grid-template-columns: auto 64px minmax(0, 1fr);
      align-items: center;
      justify-content: initial;
      gap: 12px;
      min-width: 0;
      min-height: 0;
      margin: 0 0 16px;
      padding: 0;
      background: var(--mc-surface);
      border: 0;
    }
    .mc-staff-person-face { width: 64px; height: 64px; font-size: 1.5rem; }
    .mc-staff-person-heading { min-width: 0; }
    .mc-staff-person-heading h3 { font-size: 1.5rem !important; line-height: 1.15; overflow-wrap: anywhere; }
    .mc-staff-person-heading p { margin-top: 4px !important; color: var(--mc-ink-muted); font-size: 0.875rem; overflow-wrap: anywhere; }
    .mc-staff-answer { margin: 0 0 12px; padding: 12px; border: 1px solid var(--mc-line); border-radius: var(--mc-radius); background: var(--mc-surface); min-width: 0; }
    .mc-staff-answer h4 { margin: 0 0 4px; font-size: 1.125rem; line-height: 1.2; }
    .mc-staff-answer p { overflow-wrap: anywhere; }
    .mc-staff-evidence { margin-top: 16px; border-top: 1px solid var(--mc-line); min-width: 0; }
    .mc-staff-evidence > summary { display: flex; align-items: center; min-height: var(--mc-target); padding: 10px 2px; color: var(--mc-ink); font-size: 0.875rem; font-weight: 700; cursor: pointer; }
    .mc-staff-evidence-body { min-width: 0; overflow-wrap: anywhere; }
    .mc-staff-evidence-block { margin: 0 0 10px; padding: 10px; border: 1px solid var(--mc-line); border-radius: var(--mc-radius); background: var(--mc-surface-raised); min-width: 0; }
    .mc-staff-evidence-block strong { display: block; margin-bottom: 4px; font-size: 0.75rem; color: var(--mc-ink-muted); }
    .mc-staff-evidence-block p { overflow-wrap: anywhere; }
    .mc-staff-empty { color: var(--mc-ink-muted); }

    body.mc-staff-mode, body.mc-staff-mode main { background: var(--mc-bg); }
    body.mc-staff-mode > nav.mc-viewnav,
    body.mc-staff-mode #generated,
    body.mc-staff-mode #refresh-seconds,
    body.mc-staff-mode #refresh { display: none; }

    @media (max-width: 430px) {
      #staff-roster { grid-template-columns: 1fr; }
      .mc-staff-card { grid-template-columns: 44px minmax(0, 1fr); gap: 10px; padding-left: 10px; padding-right: 10px; }
      .mc-staff-face { width: 44px; height: 44px; }
      .mc-staff-person-header { grid-template-columns: auto 52px minmax(0, 1fr); gap: 8px; }
      .mc-staff-person-face { width: 52px; height: 52px; }
      .mc-staff-person-heading h3 { font-size: 1.25rem !important; }
      .mc-staff-evidence-block { overflow-wrap: anywhere; }
    }

    @media (max-width: 320px) {
      #staff-view #staff-roster, #staff-view #staff-person { width: 100%; min-width: 0; }
      .mc-staff-person-header { grid-template-columns: auto 44px minmax(0, 1fr); }
      .mc-staff-person-face { width: 44px; height: 44px; }
    }

    /* Someone who has asked the operating system to stop moving things is not
       asking for a shorter animation. The cockpit stops animating entirely. */
    @media (prefers-reduced-motion: reduce) {
      .mc-viewnav button, .mc-viewnav a, .mc-filters button, .mc-card, .mc-gate, .mc-burn, .mc-detail, .mc-detail-backdrop {
        transition: none;
        animation: none;
        scroll-behavior: auto;
      }
    }
`
