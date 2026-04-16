/**
 * API client with environment-aware base URL.
 *
 * Local dev:  VITE_API_BASE_URL is empty → requests go to /api/*
 *             (Vite proxy forwards to localhost:8000)
 * Staging:    VITE_API_BASE_URL=https://api-staging.example.com
 * Production: VITE_API_BASE_URL=https://api.example.com
 */

declare global {
  interface Window {
    __APP_CONFIG__?: {
      API_BASE_URL?: string
    }
  }
}

function normalizeBaseUrl(baseUrl: string): string {
  if (!baseUrl) {
    return ''
  }
  return baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl
}

const runtimeBaseUrl = (window.__APP_CONFIG__?.API_BASE_URL || '').trim()
const envBaseUrl = (import.meta.env.VITE_API_BASE_URL || '').trim()

export const API_BASE_URL = normalizeBaseUrl(runtimeBaseUrl || envBaseUrl)

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(message: string, status: number, body: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

function formatValidationPath(location: unknown) {
  if (!Array.isArray(location) || location.length === 0) {
    return ''
  }

  return location
    .map((part) => String(part))
    .join('.')
}

function collectDetailMessages(detail: unknown): string[] {
  if (typeof detail === 'string' && detail.trim()) {
    return [detail]
  }

  if (!Array.isArray(detail)) {
    return []
  }

  return detail.flatMap((item) => {
    if (typeof item === 'string' && item.trim()) {
      return [item]
    }

    if (typeof item === 'object' && item !== null) {
      const message = 'msg' in item && typeof (item as { msg?: unknown }).msg === 'string'
        ? (item as { msg: string }).msg.trim()
        : ''
      const location = 'loc' in item ? formatValidationPath((item as { loc?: unknown }).loc) : ''

      if (message && location) {
        return [`${location}: ${message}`]
      }

      if (message) {
        return [message]
      }
    }

    return []
  })
}

export function getApiErrorMessages(error: unknown): string[] {
  if (isApiError(error)) {
    if (typeof error.body === 'string' && error.body.trim()) {
      return [error.body]
    }

    if (typeof error.body === 'object' && error.body !== null && 'detail' in error.body) {
      const detailMessages = collectDetailMessages((error.body as { detail?: unknown }).detail)
      if (detailMessages.length > 0) {
        return detailMessages
      }
    }

    if (error.message.trim()) {
      return [error.message]
    }
  }

  if (error instanceof Error && error.message.trim()) {
    return [error.message]
  }

  return []
}

export function getApiErrorMessage(error: unknown, fallback = 'Request failed.') {
  return getApiErrorMessages(error)[0] ?? fallback
}

function buildRequestBody(body: BodyInit | object | undefined) {
  if (
    body === undefined ||
    body instanceof FormData ||
    typeof body === 'string' ||
    body instanceof URLSearchParams ||
    body instanceof Blob ||
    body instanceof ArrayBuffer ||
    ArrayBuffer.isView(body) ||
    (typeof ReadableStream !== 'undefined' && body instanceof ReadableStream)
  ) {
    return body
  }

  return JSON.stringify(body)
}

function hasNoResponseBody(response: Response, method?: string) {
  if (method?.toUpperCase() === 'HEAD') {
    return true
  }

  return response.status === 204 || response.status === 205 || response.headers.get('Content-Length') === '0'
}

async function readResponseBody<T>(response: Response, method?: string): Promise<T> {
  if (hasNoResponseBody(response, method)) {
    return undefined as T
  }

  const rawBody = await response.text()
  if (!rawBody.trim()) {
    return undefined as T
  }

  const contentType = response.headers.get('Content-Type') || ''
  if (contentType.includes('application/json')) {
    return JSON.parse(rawBody) as T
  }

  return rawBody as T
}

/** Optional hook for injecting per-request headers (e.g. operator tokens). */
type HeaderProvider = (path: string) => Record<string, string> | undefined
const _headerProviders: HeaderProvider[] = []

export function registerHeaderProvider(provider: HeaderProvider) {
  _headerProviders.push(provider)
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  const url = `${API_BASE_URL}${normalizedPath}`

  // Merge any headers from registered providers.
  let mergedInit = init
  for (const provider of _headerProviders) {
    const extra = provider(normalizedPath)
    if (extra) {
      const existingHeaders = mergedInit?.headers
      mergedInit = {
        ...mergedInit,
        headers: { ...Object.fromEntries(new Headers(existingHeaders as HeadersInit).entries()), ...extra },
      }
    }
  }

  const response = await fetch(url, {
    ...mergedInit,
  })

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    let errorBody: unknown = null

    try {
      errorBody = await readResponseBody(response, init?.method)
      if (
        typeof errorBody === 'object' &&
        errorBody !== null &&
        'detail' in errorBody &&
        typeof (errorBody as { detail?: unknown }).detail === 'string' &&
        (errorBody as { detail: string }).detail.trim()
      ) {
        detail = (errorBody as { detail: string }).detail
      } else if (typeof errorBody === 'object' && errorBody !== null && 'detail' in errorBody) {
        const detailMessages = collectDetailMessages((errorBody as { detail?: unknown }).detail)
        if (detailMessages.length > 0) {
          detail = detailMessages[0]
        }
      } else if (typeof errorBody === 'string' && errorBody.trim()) {
        detail = errorBody
      }
    } catch {
      // Ignore response parsing failures and keep the HTTP status text.
    }

    throw new ApiError(detail, response.status, errorBody)
  }

  return readResponseBody<T>(response, init?.method)
}

export async function apiUpload<T>(path: string, body: FormData, init?: RequestInit): Promise<T> {
  return apiFetch<T>(path, {
    ...init,
    method: init?.method || 'POST',
    body,
  })
}

export async function apiRequest<T>(
  path: string,
  method: 'GET' | 'POST' | 'PATCH' | 'DELETE',
  body?: BodyInit | object,
  init?: RequestInit,
) {
  const headers = new Headers(init?.headers)
  const requestBody = buildRequestBody(body)

  if (
    body !== undefined &&
    !(body instanceof FormData) &&
    !(typeof body === 'string') &&
    !(body instanceof URLSearchParams) &&
    !(body instanceof Blob) &&
    !(body instanceof ArrayBuffer) &&
    !ArrayBuffer.isView(body) &&
    !(typeof ReadableStream !== 'undefined' && body instanceof ReadableStream) &&
    !headers.has('Content-Type')
  ) {
    headers.set('Content-Type', 'application/json')
  }

  return apiFetch<T>(path, {
    ...init,
    headers,
    method,
    body: requestBody,
  })
}

export async function apiPost<T>(path: string, body?: BodyInit | object, init?: RequestInit) {
  return apiRequest<T>(path, 'POST', body, init)
}

export async function apiPatch<T>(path: string, body?: BodyInit | object, init?: RequestInit) {
  return apiRequest<T>(path, 'PATCH', body, init)
}

export async function apiDelete<T>(path: string, init?: RequestInit) {
  return apiRequest<T>(path, 'DELETE', undefined, init)
}
