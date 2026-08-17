export type Principal = { user_id: string; tenant_id: string; username: string; role: 'admin' | 'user' }
export type KnowledgeBase = { id: string; name: string; description?: string; is_enabled: boolean }
export type Citation = { doc_id: string; source: string; title?: string; uri?: string; score: number; metadata: Record<string, unknown> }
export type AskResult = { question: string; answer: string; mode: string; confidence?: Record<string, unknown>; retrieved: Citation[] }
export type Conversation = { conversation_id: string; title?: string; updated_at: string }
export type ConversationDetail = { conversation_id: string; title?: string; messages: Array<{ role: 'user' | 'assistant'; content: string }> }
export type JobStatus = { job_id: string; conversation_id: string; status: string; answer?: { answer_id: string } }

function cookie(name: string): string | undefined {
  return document.cookie.split('; ').find((row) => row.startsWith(`${name}=`))?.split('=')[1]
}

export class ApiClient {
  private accessToken?: string
  constructor(private readonly baseUrl = '') {}

  private async request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
    const headers = new Headers(init.headers)
    if (init.body) headers.set('Content-Type', 'application/json')
    if (this.accessToken) headers.set('Authorization', `Bearer ${this.accessToken}`)
    const response = await fetch(`${this.baseUrl}${path}`, { ...init, headers, credentials: 'include' })
    if (response.status === 401 && retry && path !== '/v1/auth/refresh') {
      if (await this.refresh()) return this.request<T>(path, init, false)
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({ detail: 'Request failed' }))
      throw new Error(typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail))
    }
    if (response.status === 204) return undefined as T
    return response.json() as Promise<T>
  }

  async login(tenant_slug: string, username: string, password: string): Promise<Principal> {
    const tokens = await this.request<{ access_token: string }>('/v1/auth/login', {
      method: 'POST', body: JSON.stringify({ tenant_slug, username, password, use_cookie: true }),
    })
    this.accessToken = tokens.access_token
    return this.me()
  }

  async refresh(): Promise<boolean> {
    const csrf = cookie('rag_csrf')
    if (!csrf) return false
    try {
      const tokens = await this.request<{ access_token: string }>('/v1/auth/refresh', {
        method: 'POST', headers: { 'X-CSRF-Token': decodeURIComponent(csrf) }, body: '{}',
      }, false)
      this.accessToken = tokens.access_token
      return true
    } catch { this.accessToken = undefined; return false }
  }

  async logout(): Promise<void> {
    const csrf = cookie('rag_csrf') ?? ''
    await this.request('/v1/auth/logout', {
      method: 'POST', headers: { 'X-CSRF-Token': decodeURIComponent(csrf) }, body: '{}',
    }, false).catch(() => undefined)
    this.accessToken = undefined
  }

  me() { return this.request<Principal>('/v1/auth/me') }
  conversation(id: string) { return this.request<ConversationDetail>(`/v1/conversations/${id}`) }
  job(id: string) { return this.request<JobStatus>(`/v1/inference-jobs/${id}`) }
  list<T>(path: string) { return this.request<T[]>(path) }
  mutate<T>(path: string, method: string, body?: unknown, idempotencyKey?: string) {
    const headers: Record<string, string> = {}
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
    return this.request<T>(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
  }

  async streamAsk(
    question: string,
    knowledgeBaseId: string,
    onEvent: (event: { type: string; data: unknown; job_id?: string }) => void,
    signal: AbortSignal,
  ): Promise<void> {
    const idempotencyKey = crypto.randomUUID()
    let lastEventId = ''
    for (let reconnect = 0; reconnect < 3; reconnect += 1) {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json', Authorization: `Bearer ${this.accessToken ?? ''}`,
        'Idempotency-Key': idempotencyKey,
      }
      if (lastEventId) headers['Last-Event-ID'] = lastEventId
      const response = await fetch(`${this.baseUrl}/ask/stream`, {
        method: 'POST', credentials: 'include', signal, headers,
        body: JSON.stringify({ question, knowledge_base_id: knowledgeBaseId }),
      })
      if (response.status === 401 && await this.refresh()) continue
      if (!response.ok || !response.body) throw new Error(`Streaming failed (${response.status})`)
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
      let buffer = ''; let terminal = false
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += value
        const events = buffer.split('\n\n'); buffer = events.pop() ?? ''
        for (const raw of events) {
          const id = raw.split('\n').find((line) => line.startsWith('id: '))?.slice(4)
          if (id) lastEventId = id
          const data = raw.split('\n').find((line) => line.startsWith('data: '))?.slice(6)
          if (data) {
            const parsed = JSON.parse(data) as { type: string; data: unknown; job_id?: string }
            onEvent(parsed)
            terminal ||= ['done', 'completed', 'failed'].includes(parsed.type)
          }
        }
      }
      if (terminal || signal.aborted) return
    }
    throw new Error('Streaming connection could not be resumed')
  }
}

export const api = new ApiClient()
