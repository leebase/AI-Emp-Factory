package httpapi

// cockpitScriptAssets is the single assembly point for the cockpit's browser
// script. The asset files behind it are split by responsibility, not by size:
// reading the accepted artifact, rendering the fleet, the modal reading-session
// lifecycle, the held snapshot's own currency badge, attributed detail
// presentation, source and evidence rendering, and interaction wiring.
//
// Order is presentational only. Every declaration is a hoisted function or a
// module-level var that is read at call time, so the assets may be concatenated
// in any order; this one reads top-down from data to interaction.
func cockpitScriptAssets() string {
	return cockpitStateJS + cockpitRenderJS + cockpitModalJS + cockpitCurrencyJS +
		cockpitDetailJS + cockpitSourcesJS + cockpitHistoryJS + cockpitHistoryLineageJS + cockpitHistoryLifecycleJS +
		cockpitBoardHistoryJS + cockpitChallengesJS + cockpitChallengesRenderJS + cockpitStaffJS + cockpitWiringJS
}

// cockpitStyleSheet is the single assembly point for the cockpit's stylesheet.
// Order is the cascade the layers depend on: the token contract is declared
// first, then the shell and typography that consume it, then composition.
// Splitting is by responsibility, not by size, and no layer may reintroduce a
// literal colour: a theme override replaces the first layer's values only.
func cockpitStyleSheet() string {
	return cockpitThemeCSS + cockpitShellCSS + cockpitLayoutCSS
}
