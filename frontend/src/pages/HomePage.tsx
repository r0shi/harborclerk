import ChatPage from './ChatPage'

/**
 * Home route ('/'). Renders the chat experience unconditionally; the
 * empty-corpus signaling is handled globally by Layout.tsx's
 * <CorpusEmptyBanner /> and the first-run <OnboardingWizard />.
 *
 * Stage 2 dropped the previous "redirect to /upload when doc count is
 * zero" behaviour — that route is gone, and yanking users off chat on
 * arrival was confusing once direct upload became internal-only.
 */
export default function HomePage() {
  return <ChatPage />
}
