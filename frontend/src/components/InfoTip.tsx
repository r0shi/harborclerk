import { useEffect, useRef, useState } from 'react'

export function InfoTip({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onMouseDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div className="relative inline-block" ref={ref}>
      <button
        type="button"
        aria-label="More info"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="ml-1.5 inline-flex h-4 w-4 items-center justify-center rounded-full text-[11px] leading-none text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
      >
        &#9432;
      </button>
      {open && (
        <div className="absolute z-20 left-0 top-6 w-64 rounded-lg bg-white dark:bg-[#2c2c2e] shadow-mac-lg p-3 text-xs leading-relaxed text-gray-600 dark:text-gray-300">
          {text}
        </div>
      )}
    </div>
  )
}
