// Grouping of the model list by the Mac each model needs (#556). The API
// gives min_ram_gb per model; this only orders and labels.

export interface RamTiered {
  min_ram_gb: number
  memory_bytes: number
}

export function ramTierLabel(gb: number): string {
  return `For Macs with ${gb} GB or more`
}

export function groupModelsByRam<T extends RamTiered>(models: T[]): { gb: number; label: string; items: T[] }[] {
  return Array.from(new Set(models.map((m) => m.min_ram_gb)))
    .sort((a, b) => a - b)
    .map((gb) => ({
      gb,
      label: ramTierLabel(gb),
      items: models.filter((m) => m.min_ram_gb === gb).sort((a, b) => a.memory_bytes - b.memory_bytes),
    }))
}
