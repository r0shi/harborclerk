// Color palette covering all spaCy entity types we might see. Grouped by
// semantic kind so related types share a hue family:
//   blue/indigo:  people + groups (PERSON, NORP)
//   green/teal:   organizations + products (ORG, PRODUCT, WORK_OF_ART)
//   amber/orange: places (GPE, LOC, FAC)
//   cyan/sky:     time (DATE, TIME)
//   pink/rose:    events + law (EVENT, LAW)
//   violet/fuchsia: language + culture (LANGUAGE)
//   lime/green:   money/percent (MONEY, PERCENT)
//   stone:        numbers (CARDINAL, ORDINAL, QUANTITY)
export const ENTITY_TYPE_COLORS: Record<string, string> = {
  PERSON: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  NORP: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400',
  ORG: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  PRODUCT: 'bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400',
  WORK_OF_ART: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
  GPE: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  LOC: 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400',
  FAC: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400',
  DATE: 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-400',
  TIME: 'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-400',
  EVENT: 'bg-pink-100 text-pink-700 dark:bg-pink-900/30 dark:text-pink-400',
  LAW: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400',
  LANGUAGE: 'bg-violet-100 text-violet-700 dark:bg-violet-900/30 dark:text-violet-400',
  MONEY: 'bg-lime-100 text-lime-700 dark:bg-lime-900/30 dark:text-lime-400',
  PERCENT: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  CARDINAL: 'bg-stone-100 text-stone-700 dark:bg-stone-800/40 dark:text-stone-300',
  ORDINAL: 'bg-stone-100 text-stone-700 dark:bg-stone-800/40 dark:text-stone-300',
  QUANTITY: 'bg-stone-100 text-stone-700 dark:bg-stone-800/40 dark:text-stone-300',
}

export const DEFAULT_HIDDEN_ENTITY_TYPES = new Set(['CARDINAL', 'ORDINAL', 'QUANTITY'])

/** Returns the Tailwind class string for an entity type, with a sensible gray fallback. */
export function entityTypeClass(type: string): string {
  return ENTITY_TYPE_COLORS[type] || 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
}
