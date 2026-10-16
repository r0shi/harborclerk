import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

const nativeDialogs = ['confirm', 'alert', 'prompt']
const nativeDialogMessage =
  'Native dialogs silently return false in the macOS app (WKWebView). Use an in-page two-click confirm.'

export default tseslint.config(
  { ignores: ['dist'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
      // The macOS app hosts this SPA in a WKWebView, where native dialogs silently return false
      // (frontend/AGENTS.md, "No native dialogs"); use an in-page two-click confirm instead.
      'no-restricted-globals': ['error', ...nativeDialogs.map((name) => ({ name, message: nativeDialogMessage }))],
      'no-restricted-properties': [
        'error',
        ...['window', 'globalThis', 'self'].flatMap((object) =>
          nativeDialogs.map((property) => ({ object, property, message: nativeDialogMessage })),
        ),
      ],
    },
  },
)
