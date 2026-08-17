import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { api, Citation, Conversation, KnowledgeBase, Principal } from './api'

type Message = { role: 'user' | 'assistant'; text: string; citations?: Citation[]; details?: unknown; answerId?: string }

function Login({ onLogin }: { onLogin: (principal: Principal) => void }) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('')
    const values = new FormData(event.currentTarget)
    try { onLogin(await api.login(String(values.get('tenant')), String(values.get('username')), String(values.get('password')))) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Не удалось войти') }
    finally { setBusy(false) }
  }
  return <main className="login-shell"><form className="card login" onSubmit={submit}>
    <p className="eyebrow">PRIVATE KNOWLEDGE</p><h1>RAG Assistant</h1>
    <p className="muted">Документация, код и схемы данных — в одном защищённом пространстве.</p>
    <label>Tenant<input name="tenant" autoComplete="organization" required /></label>
    <label>Логин<input name="username" autoComplete="username" required /></label>
    <label>Пароль<input name="password" type="password" autoComplete="current-password" required /></label>
    {error && <p className="error" role="alert">{error}</p>}
    <button disabled={busy}>{busy ? 'Входим…' : 'Войти'}</button>
  </form></main>
}

function Chat() {
  const [bases, setBases] = useState<KnowledgeBase[]>([])
  const [base, setBase] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [history, setHistory] = useState<Conversation[]>([])
  const [historyVersion, setHistoryVersion] = useState(0)
  const [question, setQuestion] = useState('')
  const [error, setError] = useState('')
  const controller = useRef<AbortController | null>(null)
  const [currentJob, setCurrentJob] = useState<string>()
  useEffect(() => { api.list<KnowledgeBase>('/v1/knowledge-bases').then((rows) => { setBases(rows); setBase(rows[0]?.id ?? '') }).catch(() => undefined) }, [])
  useEffect(() => { api.list<Conversation>('/v1/conversations').then(setHistory).catch(() => undefined) }, [historyVersion])
  async function openConversation(id: string) {
    try { const item = await api.conversation(id); setMessages(item.messages.map((row) => ({ role: row.role, text: row.content }))) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'История недоступна') }
  }
  async function rate(answerId: string, rating: -1 | 1) {
    try { await api.mutate(`/v1/answers/${answerId}/feedback`, 'POST', { rating }) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Feedback не сохранён') }
  }
  async function ask(event: FormEvent) {
    event.preventDefault(); if (!question.trim() || !base) return
    const prompt = question.trim(); setQuestion(''); setError('')
    setMessages((rows) => [...rows, { role: 'user', text: prompt }, { role: 'assistant', text: '' }])
    controller.current = new AbortController()
    try {
      await api.streamAsk(prompt, base, (event) => {
        if (event.job_id) setCurrentJob(event.job_id)
        setMessages((rows) => {
          const copy = [...rows]; const last = { ...copy[copy.length - 1] }
          if (event.type === 'token') last.text += String(event.data)
          if (event.type === 'meta') { const data = event.data as { retrieved?: Citation[] }; last.citations = data.retrieved; last.details = data }
          copy[copy.length - 1] = last; return copy
        })
        if (event.type === 'error' || event.type === 'failed') setError(String(event.data))
        if ((event.type === 'done' || event.type === 'completed') && event.job_id) {
          setHistoryVersion((value) => value + 1)
          void api.job(event.job_id).then((job) => setMessages((current) => {
            const updated = [...current]; const answer = { ...updated[updated.length - 1] }
            answer.answerId = job.answer?.answer_id; updated[updated.length - 1] = answer; return updated
          }))
        }
      }, controller.current.signal)
    } catch (reason) { if (!controller.current?.signal.aborted) setError(reason instanceof Error ? reason.message : 'Сеть недоступна') }
  }
  async function cancel() { controller.current?.abort(); if (currentJob) await api.mutate(`/v1/inference-jobs/${currentJob}/cancel`, 'POST').catch(() => undefined) }
  return <section className="workspace" aria-label="Чат">
    <header><div><p className="eyebrow">GROUNDED CHAT</p><h2>Ассистент</h2></div>
      <label className="compact">База знаний<select value={base} onChange={(e) => setBase(e.target.value)}>{bases.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label></header>
    <div className="history"><strong>История</strong>{history.map((item) => <button className="secondary" key={item.conversation_id} onClick={() => void openConversation(item.conversation_id)}>{item.title || 'Диалог'}</button>)}</div>
    <div className="messages" aria-live="polite">{messages.length === 0 && <div className="empty"><h3>Начните с вопроса</h3><p>Ответ будет связан с активной версией выбранной базы знаний.</p></div>}
      {messages.map((message, index) => <article key={index} className={`message ${message.role}`}><strong>{message.role === 'user' ? 'Вы' : 'Ассистент'}</strong><p>{message.text || '…'}</p>
        {!!message.citations?.length && <details><summary>Источники ({message.citations.length})</summary><ol>{message.citations.map((source) => <li key={source.doc_id}>{source.uri ? <a href={source.uri} target="_blank" rel="noreferrer">{source.title ?? source.doc_id}</a> : source.title ?? source.doc_id}<small>{source.source} · {source.score.toFixed(4)}</small></li>)}</ol></details>}
        {message.answerId && <div className="feedback" aria-label="Оценка ответа"><button className="secondary" onClick={() => void rate(message.answerId!, 1)}>Полезно</button><button className="secondary" onClick={() => void rate(message.answerId!, -1)}>Не помогло</button></div>}
        {message.details != null && <details><summary>Маршрут и SQL validation</summary><pre>{JSON.stringify(message.details, null, 2)}</pre></details>}</article>)}</div>
    {error && <p className="error" role="alert">{error}</p>}
    <form className="composer" onSubmit={ask}><textarea value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={10000} placeholder="Спросите о документации, коде или данных…" aria-label="Вопрос"/><div><button type="button" className="secondary" onClick={() => void cancel()}>Отменить</button><button disabled={!base}>Отправить</button></div></form>
  </section>
}

const resources = [
  ['Пользователи', '/v1/admin/users'], ['API keys', '/v1/api-keys'],
  ['Базы знаний', '/v1/admin/knowledge-bases'], ['Источники', '/v1/admin/knowledge-sources'],
  ['Indexing runs', '/v1/admin/indexing-runs'], ['Расписания', '/v1/admin/schedules'],
  ['Модели', '/v1/admin/models'], ['Prompts', '/v1/admin/prompts'],
  ['Audit log', '/v1/admin/audit-events'],
] as const

function Admin() {
  const [path, setPath] = useState<string>(resources[0][1]); const [rows, setRows] = useState<unknown[]>([]); const [error, setError] = useState('')
  const load = useCallback(async () => { try { setRows(await api.list<unknown>(path)); setError('') } catch (reason) { setError(reason instanceof Error ? reason.message : 'Ошибка') } }, [path])
  useEffect(() => { void load() }, [load])
  return <section className="workspace admin"><header><div><p className="eyebrow">TENANT CONTROL</p><h2>Администрирование</h2></div><button className="secondary" onClick={load}>Обновить</button></header>
    <nav className="tabs" aria-label="Административные разделы">{resources.map(([label, value]) => <button className={path === value ? 'active' : 'secondary'} key={value} onClick={() => setPath(value)}>{label}</button>)}</nav>
    {error && <p className="error">{error}</p>}<div className="data-list">{rows.length === 0 ? <p className="empty">Нет записей</p> : rows.map((row, i) => <article className="card" key={i}><pre>{JSON.stringify(row, null, 2)}</pre></article>)}</div>
    <details className="card"><summary>Операторская мутация</summary><p className="muted">Dangerous actions require confirmation. Secrets are never rendered after creation.</p><OperatorForm path={path} onDone={load}/></details>
  </section>
}

function OperatorForm({ path, onDone }: { path: string; onDone: () => void }) {
  const [body, setBody] = useState('{}'); const [error, setError] = useState('')
  async function submit(event: FormEvent) { event.preventDefault(); if (!confirm('Подтвердить операцию?')) return
    try { await api.mutate(path, 'POST', JSON.parse(body), crypto.randomUUID()); setError(''); onDone() } catch (reason) { setError(reason instanceof Error ? reason.message : 'Ошибка') } }
  return <form onSubmit={submit}><label>JSON contract<textarea value={body} onChange={(e) => setBody(e.target.value)} spellCheck={false}/></label>{error && <p className="error">{error}</p>}<button>Выполнить</button></form>
}

export function App() {
  const [principal, setPrincipal] = useState<Principal>(); const [page, setPage] = useState<'chat' | 'admin'>('chat'); const [checking, setChecking] = useState(true)
  useEffect(() => { api.refresh().then((ok) => ok ? api.me().then(setPrincipal) : undefined).finally(() => setChecking(false)) }, [])
  if (checking) return <main className="center">Проверяем сессию…</main>
  if (!principal) return <Login onLogin={setPrincipal}/>
  return <div className="app"><aside><div><p className="brand">RAG<span>•</span></p><p className="muted">{principal.username}</p></div><nav><button className={page === 'chat' ? 'active' : 'secondary'} onClick={() => setPage('chat')}>Чат</button>{principal.role === 'admin' && <button className={page === 'admin' ? 'active' : 'secondary'} onClick={() => setPage('admin')}>Admin</button>}</nav><button className="secondary" onClick={() => api.logout().then(() => setPrincipal(undefined))}>Выйти</button></aside>{page === 'chat' ? <Chat/> : <Admin/>}</div>
}
