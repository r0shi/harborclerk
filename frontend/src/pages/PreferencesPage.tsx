import { useAuth } from '../auth'

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100]

export default function PreferencesPage() {
  const { user, updatePreferences } = useAuth()
  const theme = user?.preferences?.theme || 'light'
  const pageSize = user?.preferences?.page_size || 10

  return (
    <div>
      <h1 className="mb-6 text-xl font-bold">Preferences</h1>

      <div className="space-y-6">
        <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-6">
          <h2 className="mb-1 text-sm font-medium text-gray-900 dark:text-gray-100">Theme</h2>
          <p className="mb-3 text-sm text-gray-500 dark:text-gray-400">Choose between light and dark appearance.</p>
          <div className="flex space-x-2">
            <button
              onClick={() => updatePreferences({ theme: 'light' })}
              className={`rounded-lg px-4 py-2 text-sm font-medium ${
                theme === 'light'
                  ? 'bg-blue-600 text-white shadow-xs'
                  : 'bg-(--color-bg-tertiary) text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              Light
            </button>
            <button
              onClick={() => updatePreferences({ theme: 'dark' })}
              className={`rounded-lg px-4 py-2 text-sm font-medium ${
                theme === 'dark'
                  ? 'bg-blue-600 text-white shadow-xs'
                  : 'bg-(--color-bg-tertiary) text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              Dark
            </button>
          </div>
        </div>

        <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-6">
          <h2 className="mb-1 text-sm font-medium text-gray-900 dark:text-gray-100">Default page size</h2>
          <p className="mb-3 text-sm text-gray-500 dark:text-gray-400">Number of items shown per page by default.</p>
          <select
            value={pageSize}
            onChange={(e) => updatePreferences({ page_size: Number(e.target.value) })}
            className="rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
          >
            {PAGE_SIZE_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
      </div>
    </div>
  )
}
