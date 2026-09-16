# AGENTS.md — `frontend`

React 19 · React Router 8 · Tailwind 4 · Vite 8 · ESLint 10. Built to static
files and served by FastAPI at `/`.

**Import from `react-router`, not `react-router-dom`.** The DOM package was a
re-export shim in v7 and does not exist in v8.

## Tailwind v4 is CSS-first — do not create a config file

There is **no `tailwind.config.js` and no `postcss.config.js`**, and adding one
is wrong. Configuration lives in `src/index.css`:

- `@import 'tailwindcss'` plus a `@theme {}` block for design tokens
- the `@tailwindcss/vite` plugin, **not** the PostCSS pipeline
- dark mode via `@custom-variant dark (&:is(.dark *))`, toggled by a `.dark`
  class on the root element

An agent that "notices the missing config" and generates one will break the
build. Design tokens are CSS custom properties (`--color-bg-primary`,
`--color-accent`, …) declared in `index.css`.

## ESLint rules that bite

- **`react-hooks/set-state-in-effect`** — you cannot call `setState`
  synchronously at the top of a `useEffect`. Wrap the work in an async function
  declared inside the effect and call it.
- **`react-hooks/immutability`** — you cannot reassign a `let` in the render
  body. Use `reduce()` rather than `forEach` with mutation.

## Dependency install

`.npmrc` sets `legacy-peer-deps=true`, and `docker/app.Dockerfile` copies it.
`eslint-plugin-react-hooks@7` declares a peer dependency on `eslint@^9` but
works fine with ESLint 10; without the flag `npm ci` fails in CI and in the
Docker build. `recharts` needs `react-is` installed explicitly or the Vite build
fails to resolve it.

## TypeScript stays on 6 until typescript-eslint moves

`legacy-peer-deps` does **not** cover this one. typescript-eslint checks the
TypeScript version at runtime and throws — `typescript-eslint does not support
TS 7.0` — so ESLint does not start at all. Its supported range is
`>=4.8.4 <6.1.0` in both the latest release and the canary, and the upstream
tracking issue has no target version.

Everything else already works on TypeScript 7: type check, build, Prettier and
the unit tests all pass, and `tsc --noEmit` drops from 2.2s to 0.33s. That gain
is not worth the documented workaround, which aliases `typescript` to
`@typescript/typescript6` and hides real TypeScript 7 behind a second name — two
compilers in the lockfile, and a package called `typescript` that is not the one
the build uses. It would also split type-aware lint rules (TypeScript 6) from
the build's type check (TypeScript 7) the moment anyone enables them.

`.github/dependabot.yml` therefore ignores the **7.0 line only**, so the 7.1 PR
still arrives and acts as the prompt to re-evaluate. Do not widen that to all
majors, and do not adopt TypeScript 7 piecemeal — see #637.

## Routing state loss

Navigating from `/` to `/c/:conversationId` **unmounts** the child ChatPage and
mounts a new instance — every `useState` and `useRef` is lost. Defer the
`navigate()` call until after streaming completes, rather than navigating first
and trying to preserve state.

Chat conversations live at `/c/:id` (not `/chat/`); old paths redirect.

## No native dialogs

The macOS app hosts this SPA in a WKWebView, where `window.confirm` and
`window.alert` **silently return false** rather than prompting. Use an inline
two-click confirmation pattern instead — a native dialog will appear to do
nothing.

## Feature detection

Source download is gated server-side (`allow_source_download`, off by default).
Feature-detect via `useSystemConfig().allowSourceDownload` before showing
download UI; on macOS the user-facing path is `window.harborclerk.revealInFinder`,
which never touches the API.

## Checks

`npm run lint` · `npm run format:check` · `npm run type-check` — all three run
in CI.
