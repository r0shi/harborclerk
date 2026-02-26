type TokenGetter = () => string | null
type TokenSetter = (token: string | null) => void
type OnUnauthorized = () => void

let getToken: TokenGetter = () => null
let setToken: TokenSetter = () => {}
let onUnauthorized: OnUnauthorized = () => {}

export function configureApi(
  getter: TokenGetter,
  setter: TokenSetter,
  onUnauth: OnUnauthorized,
) {
  getToken = getter
  setToken = setter
  onUnauthorized = onUnauth
}

// --- Build hash staleness detection ---
let knownBuildHash: string | null = null
let reloadToastShown = false

function checkBuildHash(response: Response) {
  const hash = response.headers.get('X-Build-Hash')
  if (!hash || hash === 'dev') return
  if (knownBuildHash && hash !== knownBuildHash && !reloadToastShown) {
    reloadToastShown = true
    showReloadToast()
  }
  knownBuildHash = hash
}

function showReloadToast() {
  const container = document.createElement('div')
  container.style.cssText =
    'position:fixed;top:16px;right:16px;z-index:10000;animation:toastSlideUp 0.3s ease-out'

  const toast = document.createElement('div')
  toast.style.cssText =
    'background:var(--color-bg-secondary, #1e293b);color:var(--color-text-primary, #f1f5f9);' +
    'padding:12px 20px;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,0.3);' +
    'display:flex;align-items:center;gap:12px;font-size:14px;font-family:system-ui'

  const msg = document.createElement('span')
  msg.textContent = 'Server updated. Reload for latest version.'

  const reloadBtn = document.createElement('button')
  reloadBtn.textContent = 'Reload'
  reloadBtn.style.cssText =
    'background:var(--color-accent, #3b82f6);color:white;border:none;' +
    'padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500'
  reloadBtn.addEventListener('click', () => window.location.reload())

  const closeBtn = document.createElement('button')
  closeBtn.textContent = '\u00d7'
  closeBtn.style.cssText =
    'background:none;border:none;color:var(--color-text-secondary, #94a3b8);' +
    'cursor:pointer;font-size:18px;line-height:1;padding:0 4px'
  closeBtn.addEventListener('click', () => container.remove())

  toast.append(msg, reloadBtn, closeBtn)
  container.appendChild(toast)
  document.body.appendChild(container)
}

async function refreshToken(): Promise<boolean> {
  try {
    const res = await fetch('/api/auth/refresh', {
      method: 'POST',
      credentials: 'include',
    })
    if (!res.ok) return false
    const data = await res.json()
    setToken(data.access_token)
    return true
  } catch {
    return false
  }
}

async function request(
  url: string,
  options: RequestInit = {},
  retry = true,
): Promise<Response> {
  const token = getToken()
  const headers = new Headers(options.headers)
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const res = await fetch(url, {
    ...options,
    headers,
    credentials: 'include',
  })

  checkBuildHash(res)

  if (res.status === 401 && retry) {
    const refreshed = await refreshToken()
    if (refreshed) {
      return request(url, options, false)
    }
    onUnauthorized()
  }

  return res
}

export async function get<T = unknown>(url: string): Promise<T> {
  const res = await request(url)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ApiError(res.status, err.detail || res.statusText)
  }
  return res.json()
}

export async function post<T = unknown>(
  url: string,
  body?: unknown,
): Promise<T> {
  const res = await request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ApiError(res.status, err.detail || res.statusText)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

export async function postForm<T = unknown>(
  url: string,
  formData: FormData,
): Promise<T> {
  const res = await request(url, {
    method: 'POST',
    body: formData,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ApiError(res.status, err.detail || res.statusText)
  }
  return res.json()
}

export async function put<T = unknown>(
  url: string,
  body?: unknown,
): Promise<T> {
  const res = await request(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ApiError(res.status, err.detail || res.statusText)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

export async function patch<T = unknown>(
  url: string,
  body?: unknown,
): Promise<T> {
  const res = await request(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ApiError(res.status, err.detail || res.statusText)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

export async function del(url: string): Promise<void> {
  const res = await request(url, { method: 'DELETE' })
  if (!res.ok && res.status !== 204) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ApiError(res.status, err.detail || res.statusText)
  }
}

export async function downloadBlob(url: string): Promise<{ blob: Blob; filename: string }> {
  const res = await request(url, { method: 'GET' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ApiError(res.status, err.detail || res.statusText)
  }
  const disposition = res.headers.get('Content-Disposition') || ''
  // Handle RFC 8187 filename*=UTF-8''encoded and plain filename="name"
  const starMatch = disposition.match(/filename\*=UTF-8''([^;\s]+)/i)
  const plainMatch = disposition.match(/filename="?([^"]+)"?/)
  const filename = starMatch ? decodeURIComponent(starMatch[1]) : (plainMatch?.[1] || 'download')
  const blob = await res.blob()
  return { blob, filename }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}
