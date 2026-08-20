import { FormEvent, useEffect, useRef, useState } from 'react'
import { api, Citation, Conversation, KnowledgeBase, Principal } from './api'
import { Admin } from './Admin'
import { SetupWizard } from './SetupWizard'
import type { SetupStatus } from './api'

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

export function App() {
  const [principal, setPrincipal] = useState<Principal>(); const [page, setPage] = useState<'chat' | 'admin'>('chat'); const [checking, setChecking] = useState(true); const [setup, setSetup] = useState<SetupStatus>()
  useEffect(() => { api.setupStatus().then(setSetup).then(() => api.refresh()).then((ok) => ok ? api.me().then(setPrincipal) : undefined).finally(() => setChecking(false)) }, [])
  if (checking) return <main className="center">Проверяем сессию…</main>
  if (setup?.required && !setup.setup_available) return <main className="login-shell"><section className="card login"><p className="eyebrow">SETUP LOCKED</p><h1>Первичная настройка отключена</h1><p className="muted">Platform administrator должен временно разрешить защищённый bootstrap через secret storage. После создания первого администратора этот вход закроется навсегда.</p></section></main>
  if (setup?.required && setup.setup_available) return <SetupWizard initial={setup} principal={principal} onAuthenticated={setPrincipal} onComplete={() => { setSetup({ ...setup, onboarding_complete: true, current_step: 'complete' }); setPage('admin') }}/>
  if (principal?.role === 'admin' && setup?.onboarding_complete === false) return <SetupWizard initial={setup} principal={principal} onAuthenticated={setPrincipal} onComplete={() => { setSetup({ ...setup, onboarding_complete: true, current_step: 'complete' }); setPage('admin') }}/>
  if (!principal) return <Login onLogin={setPrincipal}/>
  return <div className="app"><aside><div><p className="brand">RAG<span>•</span></p><p className="muted">{principal.username}</p></div><nav><button className={page === 'chat' ? 'active' : 'secondary'} onClick={() => setPage('chat')}>Чат</button>{principal.role === 'admin' && <button className={page === 'admin' ? 'active' : 'secondary'} onClick={() => setPage('admin')}>Администрирование</button>}</nav><button className="secondary" onClick={() => api.logout().then(() => setPrincipal(undefined))}>Выйти</button></aside>{page === 'chat' ? <Chat/> : <Admin/>}</div>
}
