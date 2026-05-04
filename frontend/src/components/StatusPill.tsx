export type PillState = 'active' | 'running' | 'idle' | 'error' | 'pending'

interface StatusPillProps {
  state: PillState
  /** Override the default label for the state (e.g. "embedding" instead of "running"). */
  label?: string
  /** Override the default glyph for the state. */
  glyph?: string
}

const STATE_CONFIG: Record<PillState, { glyph: string; label: string; cls: string }> = {
  active: {
    glyph: '●',
    label: 'active',
    // Sage tints — light mode bg/fg first, dark mode after the colon.
    cls: 'bg-[rgba(74,108,73,0.10)] text-[#4a6c49] dark:bg-[rgba(122,150,112,0.18)] dark:text-[#a8c49a]',
  },
  running: {
    glyph: '⟳',
    label: 'running',
    cls: 'bg-[rgba(150,108,46,0.10)] text-[#966c2e] dark:bg-[rgba(184,138,74,0.18)] dark:text-[#d4a574]',
  },
  idle: {
    glyph: '○',
    label: 'idle',
    cls: 'bg-[rgba(90,90,100,0.10)] text-[#5a5a64] dark:bg-[rgba(122,122,130,0.18)] dark:text-[#a0a0a8]',
  },
  error: {
    glyph: '⚠',
    label: 'error',
    cls: 'bg-[rgba(168,90,90,0.10)] text-[#a85a5a] dark:bg-[rgba(168,90,90,0.20)] dark:text-[#d49a9a]',
  },
  pending: {
    glyph: '◐',
    label: 'pending',
    cls: 'bg-[rgba(63,104,133,0.10)] text-[#3f6885] dark:bg-[rgba(91,138,168,0.18)] dark:text-[#8aafc4]',
  },
}

/**
 * Status pill — leading glyph + label, with state-tinted background and
 * matching foreground color in both light and dark modes.
 */
export function StatusPill({ state, label, glyph }: StatusPillProps) {
  const cfg = STATE_CONFIG[state]
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${cfg.cls}`}
    >
      <span aria-hidden>{glyph ?? cfg.glyph}</span>
      <span>{label ?? cfg.label}</span>
    </span>
  )
}
