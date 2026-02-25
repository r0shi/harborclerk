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

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}
