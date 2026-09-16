// The `/vitest` subpath, not the bare package. Vitest 5 changed `Assertion` to
// take two type parameters, and only this entry point augments the new shape —
// the bare import left all 164 jest-dom matcher calls as TS2339 "Property
// 'toBeInTheDocument' does not exist". The matchers still worked at runtime,
// so `npm test` stayed green while `tsc --noEmit` and the Docker build failed.
import '@testing-library/jest-dom/vitest'
