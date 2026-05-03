/**
 * Native bridge surface exposed by the macOS WKWebView app via
 * window.harborclerk. Callable from any page that runs inside the bundled
 * Mac client; absent in any other browser (regular Chrome/Safari, the
 * Docker web UI, etc.). Always feature-detect before calling, e.g.:
 *
 *     if (window.harborclerk?.revealInFinder) { ... }
 *
 * The Swift side registers each handler in macos/HarborClerk/HarborClerk/
 * ContentView.swift and injects the JS shim that maps these calls onto
 * window.webkit.messageHandlers.<channel>.postMessage(...).
 */
declare global {
  interface Window {
    harborclerk?: {
      /**
       * Open a native NSOpenPanel and resolve to the chosen absolute path,
       * or null if the user cancelled. Used by the watched-folder picker.
       *
       * Required when `harborclerk` is present — every shipped Mac client
       * registers this handler; absence of `pickFolder` would mean we're
       * not really running in the bridge environment.
       */
      pickFolder: () => Promise<string | null>
      /**
       * Reveal the file at the given absolute host path in Finder. Returns
       * a Promise that resolves once the message has been delivered to the
       * Swift side.
       *
       * Optional because older Mac clients (pre-revealInFinder bridge) may
       * have `harborclerk.pickFolder` but not this method. Always
       * feature-detect: `if (window.harborclerk?.revealInFinder) { ... }`.
       */
      revealInFinder?: (path: string) => Promise<void>
    }
  }
}

export {}
