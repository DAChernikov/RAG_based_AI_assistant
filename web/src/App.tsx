import { FormEvent, useEffect, useRef, useState } from 'react'
import { Admin } from './Admin'
import { Guide } from './Guide'
import { SetupWizard } from './SetupWizard'
import {
  api,
  Citation,
  Conversation,
  KnowledgeBase,
  Principal,
  readableError,
  SetupStatus,
} from './api'

type Page = 'chat' | 'guide' | 'admin' | 'profile'
type Theme = 'light' | 'dark'
type Message = {
  role: 'user' | 'assistant'
  text: string
  citations?: Citation[]
  details?: unknown
  answerId?: string
}

function storedTheme(): Theme {
  try {
    return typeof localStorage?.getItem === 'function' && localStorage.getItem('rag_theme') === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

function Login({ onLogin }: { onLogin: (principal: Principal) => void }) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError('')
    const values = new FormData(event.currentTarget)
    try {
      onLogin(await api.login(String(values.get('tenant')), String(values.get('username')), String(values.get('password'))))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось войти')
    } finally {
      setBusy(false)
    }
  }
  return <main className="login-shell"><form className="card login" onSubmit={submit}>
    <div className="logo-mark" aria-hidden="true">R</div><p className="eyebrow">PRIVATE AI WORKSPACE</p><h1>RAG Assistant</h1>
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
  const [isStreaming, setIsStreaming] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const [currentJob, setCurrentJob] = useState<string>()

  useEffect(() => {
    api.list<KnowledgeBase>('/v1/knowledge-bases').then(setBases).catch(() => undefined)
  }, [])
  useEffect(() => {
    api.list<Conversation>('/v1/conversations').then(setHistory).catch(() => undefined)
  }, [historyVersion])

  async function openConversation(id: string) {
    try {
      const item = await api.conversation(id)
      setMessages(item.messages.map((row) => ({ role: row.role, text: row.content })))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'История недоступна')
    }
  }
  async function rate(answerId: string, rating: -1 | 1) {
    try {
      await api.mutate(`/v1/answers/${answerId}/feedback`, 'POST', { rating })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Feedback не сохранён')
    }
  }
  async function ask(event: FormEvent) {
    event.preventDefault()
    if (!question.trim() || isStreaming) return
    const prompt = question.trim()
    setQuestion('')
    setError('')
    setIsStreaming(true)
    setMessages((rows) => [...rows, { role: 'user', text: prompt }, { role: 'assistant', text: '' }])
    controller.current = new AbortController()
    try {
      await api.streamAsk(prompt, base || undefined, (streamEvent) => {
        if (streamEvent.job_id) setCurrentJob(streamEvent.job_id)
        setMessages((rows) => {
          const copy = [...rows]
          const last = { ...copy[copy.length - 1] }
          if (streamEvent.type === 'token') last.text += String(streamEvent.data ?? '')
          if (streamEvent.type === 'meta') {
            const data = streamEvent.data as { retrieved?: Citation[] }
            last.citations = data.retrieved
            last.details = data
          }
          copy[copy.length - 1] = last
          return copy
        })
        if (streamEvent.type === 'error' || streamEvent.type === 'failed') setError(readableError(streamEvent.data))
        if (['done', 'completed', 'failed'].includes(streamEvent.type)) setIsStreaming(false)
        if ((streamEvent.type === 'done' || streamEvent.type === 'completed') && streamEvent.job_id) {
          setHistoryVersion((value) => value + 1)
          void api.job(streamEvent.job_id).then((job) => setMessages((current) => {
            const updated = [...current]
            const answer = { ...updated[updated.length - 1], answerId: job.answer?.answer_id }
            updated[updated.length - 1] = answer
            return updated
          }))
        }
      }, controller.current.signal)
    } catch (reason) {
      if (!controller.current?.signal.aborted) setError(reason instanceof Error ? reason.message : 'Сеть недоступна')
    } finally {
      setIsStreaming(false)
    }
  }
  async function cancel() {
    controller.current?.abort()
    setIsStreaming(false)
    if (currentJob) await api.mutate(`/v1/inference-jobs/${currentJob}/cancel`, 'POST').catch(() => undefined)
  }

  return <section className="workspace chat-page" aria-label="Чат">
    <header className="page-header"><div><p className="eyebrow">AI WORKSPACE</p><h1>Ассистент</h1><p className="lead">Спросите модель напрямую или подключите проверяемый контекст.</p></div>
      <label className="context-picker"><span>Контекст ответа</span><select value={base} onChange={(event) => setBase(event.target.value)}><option value="">Без базы знаний · знания модели</option>{bases.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}</select><small>{base ? 'Ответ с retrieval и citations' : 'Retrieval отключён, ссылки не создаются'}</small></label></header>
    <div className="chat-layout">
      <aside className="conversation-list"><div className="section-title"><strong>История</strong><span>{history.length}</span></div>{history.length === 0 ? <p className="empty-copy">Здесь появятся диалоги</p> : history.map((item) => <button className="conversation-item" key={item.conversation_id} onClick={() => void openConversation(item.conversation_id)}><span>{item.title || 'Новый диалог'}</span><small>{new Date(item.updated_at).toLocaleDateString()}</small></button>)}</aside>
      <div className="chat-column">
        <div className="messages" aria-live="polite">{messages.length === 0 && <div className="empty chat-empty"><div className="empty-icon">✦</div><h2>С чего начнём?</h2><p>{base ? 'Ответ будет основан на активном индексе выбранной базы.' : 'Модель ответит из собственных предобученных знаний.'}</p><div className="suggestions"><button onClick={() => setQuestion('Кратко расскажи, чем ты можешь мне помочь')}>Что ты умеешь?</button><button onClick={() => setQuestion('Предложи план решения моей задачи')}>Составить план</button></div></div>}
          {messages.map((message, index) => <article key={index} className={`message ${message.role}`}><div className="message-avatar">{message.role === 'user' ? 'В' : '✦'}</div><div className="message-body"><strong>{message.role === 'user' ? 'Вы' : 'Ассистент'}</strong><p>{message.text || 'Думаю…'}</p>
            {!!message.citations?.length && <details className="citations"><summary>Источники · {message.citations.length}</summary><ol>{message.citations.map((source) => <li key={source.doc_id}>{source.uri ? <a href={source.uri} target="_blank" rel="noreferrer">{source.title ?? source.doc_id}</a> : source.title ?? source.doc_id}<small>{source.source} · {source.score.toFixed(4)}</small></li>)}</ol></details>}
            {message.answerId && <div className="feedback" aria-label="Оценка ответа"><button className="ghost" onClick={() => void rate(message.answerId!, 1)}>Полезно</button><button className="ghost" onClick={() => void rate(message.answerId!, -1)}>Не помогло</button></div>}
            {message.details != null && <details><summary>Технические детали маршрута</summary><pre>{JSON.stringify(message.details, null, 2)}</pre></details>}
          </div></article>)}</div>
        {error && <p className="error" role="alert"><strong>Не удалось получить ответ.</strong> {error}</p>}
        <form className="composer" onSubmit={ask}><textarea value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={10000} placeholder={base ? 'Спросите о выбранной базе знаний…' : 'Спросите модель без дополнительного контекста…'} aria-label="Вопрос"/><div className="composer-actions"><span>{base ? 'Контекст включён' : 'Model-only'}</span>{isStreaming && <button type="button" className="secondary" onClick={() => void cancel()}>Остановить</button>}<button disabled={!question.trim() || isStreaming}>{isStreaming ? 'Отвечает…' : 'Отправить'}</button></div></form>
      </div>
    </div>
  </section>
}

function Profile({ principal, onLogout }: { principal: Principal; onLogout: () => void }) {
  const [notice, setNotice] = useState('')
  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    const next = String(data.get('new_password'))
    if (next !== String(data.get('confirm_password'))) return setNotice('Новые пароли не совпадают.')
    try {
      await api.mutate('/v1/auth/change-password', 'POST', { current_password: data.get('current_password'), new_password: next })
      form.reset()
      setNotice('Пароль изменён. Остальные активные сессии отозваны.')
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : 'Пароль не изменён.')
    }
  }
  return <section className="workspace profile-page"><header className="page-header"><div><p className="eyebrow">ACCOUNT</p><h1>Профиль</h1><p className="lead">Настройки вашей учётной записи.</p></div></header><div className="admin-grid"><section className="card"><div className="profile-hero"><span className="avatar large">{principal.username.slice(0, 1).toUpperCase()}</span><div><h2>{principal.username}</h2><p className="muted">{principal.role === 'admin' ? 'Администратор' : 'Пользователь'} · tenant {principal.tenant_id.slice(0, 8)}</p></div></div><button className="secondary" onClick={onLogout}>Выйти из аккаунта</button></section><section className="card"><h2>Сменить пароль</h2><form className="form-grid" onSubmit={changePassword}><label>Текущий пароль<input name="current_password" type="password" autoComplete="current-password" required /></label><label>Новый пароль<input name="new_password" type="password" minLength={12} autoComplete="new-password" required /></label><label>Повторите новый пароль<input name="confirm_password" type="password" minLength={12} autoComplete="new-password" required /></label><button>Обновить пароль</button>{notice && <p className="notice" role="status">{notice}</p>}</form></section></div></section>
}

export function App() {
  const [principal, setPrincipal] = useState<Principal>()
  const [page, setPage] = useState<Page>('chat')
  const [checking, setChecking] = useState(true)
  const [setup, setSetup] = useState<SetupStatus>()
  const [theme, setTheme] = useState<Theme>(storedTheme)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    if (typeof localStorage?.setItem === 'function') localStorage.setItem('rag_theme', theme)
  }, [theme])
  useEffect(() => {
    api.setupStatus().then(setSetup).then(() => api.refresh()).then((ok) => ok ? api.me().then(setPrincipal) : undefined).finally(() => setChecking(false))
  }, [])
  const logout = () => void api.logout().then(() => setPrincipal(undefined))
  const completeSetup = () => {
    setSetup((current) => current ? {
      ...current,
      required: false,
      setup_available: false,
      onboarding_complete: true,
      current_step: 'complete',
    } : current)
    setPage('admin')
  }

  if (checking) return <main className="center">Проверяем сессию…</main>
  if (setup?.required && !setup.setup_available) return <main className="login-shell"><section className="card login"><p className="eyebrow">SETUP LOCKED</p><h1>Первичная настройка отключена</h1><p className="muted">Platform administrator должен временно разрешить защищённый bootstrap через secret storage.</p></section></main>
  if (setup?.required && setup.setup_available) return <SetupWizard initial={setup} principal={principal} onAuthenticated={setPrincipal} onComplete={completeSetup}/>
  if (principal?.role === 'admin' && setup?.onboarding_complete === false) return <SetupWizard initial={setup} principal={principal} onAuthenticated={setPrincipal} onComplete={completeSetup}/>
  if (!principal) return <Login onLogin={setPrincipal}/>

  return <div className="app">
    <aside className="sidebar">
      <div className="sidebar-brand"><span className="brand-symbol">R</span><div><strong>RAG Assistant</strong><small>Self-hosted AI</small></div></div>
      <nav aria-label="Основная навигация">
        <button aria-label="Чат" className={page === 'chat' ? 'active' : ''} onClick={() => setPage('chat')}><span>✦</span>Чат</button>
        <button aria-label="Guide" className={page === 'guide' ? 'active' : ''} onClick={() => setPage('guide')}><span>?</span>Guide</button>
        {principal.role === 'admin' && <button aria-label="Администрирование" className={page === 'admin' ? 'active' : ''} onClick={() => setPage('admin')}><span>⌘</span>Администрирование</button>}
      </nav>
      <div className="sidebar-footer">
        <button className="theme-toggle" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} aria-label="Переключить тему"><span>{theme === 'dark' ? '☀' : '☾'}</span>{theme === 'dark' ? 'Светлая тема' : 'Тёмная тема'}</button>
        <button className={`account-button ${page === 'profile' ? 'active' : ''}`} onClick={() => setPage('profile')}><span className="avatar">{principal.username.slice(0, 1).toUpperCase()}</span><span><strong>{principal.username}</strong><small>{principal.role === 'admin' ? 'Администратор' : 'Пользователь'}</small></span><b>···</b></button>
      </div>
    </aside>
    <main className="main-content">{page === 'chat' && <Chat/>}{page === 'guide' && <Guide isAdmin={principal.role === 'admin'}/>} {page === 'admin' && principal.role === 'admin' && <Admin/>}{page === 'profile' && <Profile principal={principal} onLogout={logout}/>}</main>
  </div>
}
