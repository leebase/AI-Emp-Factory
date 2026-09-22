package httpapi

// cockpitShellCSS is the application shell and typography layer: the view
// switcher that frames the whole dashboard, the cockpit surface itself, the
// currency banner, section chrome, filters and the focus indicator.
//
// Every declaration here resolves colour through the token contract in
// cockpit_theme.go. Sprint 5 left the shell nav on the light page tokens
// (--panel/--line) and then patched it back inside #cockpit-view; that is the
// kind of bypass a token override cannot reach, so it is gone.
//
// The Advanced board view keeps its own existing styles untouched. Only mc-
// prefixed chrome is themed.
const cockpitShellCSS = `
    /* The page sets a fixed 14px on <body>, which silently defeats a browser or
       OS root text-size increase for everything that inherits from it. The
       cockpit, the modal and the session controls therefore state their text in
       root-relative units: 0.875rem is the same readable default at a 16px
       root and actually doubles at a 200% root. */
    #cockpit-view, #staff-view, .mc-detail-backdrop, #session-toolbar {
      font-size: 0.875rem;
      line-height: 1.45;
    }

    /* Header session controls are cockpit controls: Lee refreshes and signs out
       from here. They were 36px, which is under the touch floor. */
    header {
      padding-top: max(18px, env(safe-area-inset-top));
      padding-left: max(clamp(16px, 4vw, 40px), env(safe-area-inset-left));
      padding-right: max(clamp(16px, 4vw, 40px), env(safe-area-inset-right));
      background: var(--mc-surface);
      color: var(--mc-ink);
      border-bottom: 1px solid var(--mc-line);
    }

    /* viewport-fit=cover exposes the cutout to the whole document, so the
       preserved Advanced content inside <main> needs its own inset floors or it
       sits under a notch. Base values are the page's existing ones; only the
       max() floor is added. */
    main {
      padding-left: max(clamp(16px, 4vw, 40px), env(safe-area-inset-left));
      padding-right: max(clamp(16px, 4vw, 40px), env(safe-area-inset-right));
      padding-bottom: max(36px, env(safe-area-inset-bottom));
    }

    header h1 { font-size: 1.375rem; }

    /* The legacy .meta foreground is a light-page token. On the themed header
       surface it would be dark text on a dark surface, so it is re-owned. */
    header .meta, #session-toolbar .meta, #session-state {
      font-size: 0.8125rem;
      color: var(--mc-ink-muted);
    }

    #session-toolbar button, #session-toolbar input, #session-toolbar a {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: var(--mc-target);
      min-width: var(--mc-target);
      padding: 10px 14px;
      font-size: 0.875rem;
      background: var(--mc-surface-raised);
      color: var(--mc-ink);
      border: 1px solid var(--mc-line-strong);
      border-radius: var(--mc-radius);
    }

    /* A display rule outranks the hidden attribute. Without these guards the
       new inline-flex leaves the sign-in link and the refresh control visible
       exactly when the cockpit has withdrawn authority for them: an
       authenticated page would still offer Sign in, and a 403 would offer both
       Sign in and Refresh. Same defect class as the modal backdrop guard, which
       is retained below. */
    .mc-viewnav [hidden],
    #session-toolbar [hidden],
    #cockpit-view [hidden], #staff-view [hidden] { display: none; }

    .mc-viewnav {
      display: flex;
      flex-wrap: wrap;
      gap: var(--mc-gap);
      padding: 12px clamp(16px, 4vw, 40px);
      padding-left: max(clamp(16px, 4vw, 40px), env(safe-area-inset-left));
      padding-right: max(clamp(16px, 4vw, 40px), env(safe-area-inset-right));
      background: var(--mc-bg);
      border-bottom: 1px solid var(--mc-line);
    }

    #cockpit-view .mc-viewnav, #staff-view .mc-viewnav {
      background: transparent;
      border-bottom: 0;
      padding: 12px 0 0;
    }

    .mc-viewnav button, .mc-viewnav a, .mc-filters button {
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
      font-size: 0.875rem;
      text-decoration: none;
      cursor: pointer;
      transition: background var(--mc-motion) linear;
    }

    .mc-viewnav button[aria-pressed="true"], .mc-filters button[aria-pressed="true"] {
      border-color: var(--mc-accent);
      font-weight: 700;
    }

    #cockpit-view, #staff-view {
      background: var(--mc-bg);
      color: var(--mc-ink);
      padding: 16px clamp(12px, 3vw, 28px) 32px;
      padding-left: max(clamp(12px, 3vw, 28px), env(safe-area-inset-left));
      padding-right: max(clamp(12px, 3vw, 28px), env(safe-area-inset-right));
      padding-bottom: max(32px, env(safe-area-inset-bottom));
      border-radius: var(--mc-radius);
    }

    #cockpit-view h2, #cockpit-view h3,
    #staff-view h2, #staff-view h3 { color: var(--mc-ink); margin: 0; font-size: 0.9375rem; }
    #cockpit-view p, #staff-view p { margin: 0; }
    #cockpit-view a, #staff-view a { color: var(--mc-accent); }

    .mc-currency {
      display: block;
      margin: 4px 0 16px;
      padding: 12px 14px;
      border: 1px solid var(--mc-line);
      border-left-width: 6px;
      border-radius: var(--mc-radius);
      background: var(--mc-surface);
      color: var(--mc-ink);
      font-weight: 600;
      overflow-wrap: anywhere;
    }

    .mc-currency[data-currency="stale"] { border-left-color: var(--mc-warn); }
    .mc-currency[data-currency="unavailable"] { border-left-color: var(--mc-bad); }
    .mc-currency[data-currency="current"] { border-left-color: var(--mc-good); }
    .mc-currency[data-currency="loading"] { border-left-color: var(--mc-accent); }

    .mc-section { margin-bottom: 20px; }

    .mc-section-head {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      justify-content: space-between;
      gap: var(--mc-gap);
      margin-bottom: var(--mc-gap);
    }

    .mc-count { color: var(--mc-ink-muted); font-size: 0.8125rem; }

    .mc-empty {
      padding: 12px 14px;
      border: 1px dashed var(--mc-line-strong);
      border-radius: var(--mc-radius);
      color: var(--mc-ink-muted);
      background: var(--mc-surface);
    }

    .mc-filters {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--mc-gap);
      margin-bottom: 12px;
    }

    .mc-filters input {
      min-height: var(--mc-target);
      flex: 1 1 200px;
      min-width: 0;
      padding: 8px 12px;
      border-radius: var(--mc-radius);
      border: 1px solid var(--mc-line-strong);
      background: var(--mc-surface);
      color: var(--mc-ink);
      font: inherit;
    }

    .mc-group-title {
      margin: 16px 0 var(--mc-gap);
      font-size: 0.875rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--mc-ink-muted);
    }

    /* Focus has to be visible on the shell, the roster and inside the modal.
       The ring is drawn with its own token so an override cannot accidentally
       reduce it to the surface colour it sits on. */
    #cockpit-view :focus-visible,
    #staff-view :focus-visible,
    .mc-viewnav :focus-visible,
    #session-toolbar :focus-visible,
    .mc-detail-backdrop :focus-visible {
      outline: 3px solid var(--mc-focus);
      outline-offset: 2px;
    }
`
