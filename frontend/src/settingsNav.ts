import type { AreaHue } from './hooks/useAreaAccent'

export interface SettingsItem {
  to: string
  label: string
  sub: string
  icon: string
  hue: AreaHue
}

export interface SettingsSection {
  label: string
  items: SettingsItem[]
}

export const SETTINGS_SECTIONS: SettingsSection[] = [
  {
    label: 'Personal',
    items: [
      {
        to: '/settings/preferences',
        label: 'Preferences',
        sub: 'Theme, page size, and account settings',
        icon: '👤',
        hue: 'settings',
      },
    ],
  },
]

export const ADMIN_SETTINGS_SECTIONS: SettingsSection[] = [
  {
    label: 'Integrations',
    items: [
      {
        to: '/settings/integrations',
        label: 'Integrations',
        sub: 'MCP, CLI, and external AI tool setup',
        icon: '🔌',
        hue: 'research',
      },
    ],
  },
  {
    label: 'Access & identity',
    items: [
      { to: '/settings/users', label: 'Users', sub: 'Manage accounts and roles', icon: '👥', hue: 'docs' },
      {
        to: '/settings/api-keys',
        label: 'API Keys',
        sub: 'Create, scope, and revoke API keys',
        icon: '🔑',
        hue: 'research',
      },
    ],
  },
  {
    label: 'Models & languages',
    items: [
      {
        to: '/settings/models',
        label: 'Models',
        sub: 'Download and manage local AI models',
        icon: '🧠',
        hue: 'observatory',
      },
      {
        to: '/settings/languages',
        label: 'Languages',
        sub: 'OCR and entity language packs',
        icon: '🌐',
        hue: 'explore',
      },
    ],
  },
  {
    label: 'Behavior & limits',
    items: [
      {
        to: '/settings/retrieval',
        label: 'Retrieval',
        sub: 'Ask, Research, MCP, and search behavior',
        icon: '🔍',
        hue: 'search',
      },
      { to: '/settings/rate-limits', label: 'Rate Limits', sub: 'Default API key rate limits', icon: '⏱', hue: 'ask' },
    ],
  },
  {
    label: 'Operations',
    items: [
      {
        to: '/settings/status',
        label: 'Status',
        sub: 'Readiness, service health, and mail account state',
        icon: '💚',
        hue: 'observatory',
      },
      {
        to: '/settings/diagnostics',
        label: 'Diagnostics',
        sub: 'Logs and advanced troubleshooting detail',
        icon: '📜',
        hue: 'settings',
      },
      {
        to: '/settings/maintenance',
        label: 'Maintenance',
        sub: 'Purge, reaper, and cleanup',
        icon: '🧹',
        hue: 'ask',
      },
      {
        to: '/settings/security',
        label: 'Security',
        sub: 'Encryption status and master key management',
        icon: '🔒',
        hue: 'research',
      },
    ],
  },
]

export function settingsSectionsForRole(isAdmin: boolean): SettingsSection[] {
  return isAdmin ? [...SETTINGS_SECTIONS, ...ADMIN_SETTINGS_SECTIONS] : SETTINGS_SECTIONS
}
