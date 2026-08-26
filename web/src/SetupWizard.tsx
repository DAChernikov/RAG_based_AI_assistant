import { FormEvent, useEffect, useState } from 'react'
import { api, Principal, SetupStatus, SystemStatus } from './api'

type Props = { initial: SetupStatus; principal?: Principal; onAuthenticated: (value: Principal) => void; onComplete: () => void }
type Notice = { kind: 'success' | 'error'; text: string } | undefined

const order = ['administrator', 'models', 'telegram', 'readiness'] as const

export function SetupWizard({ initial, principal, onAuthenticated, onComplete }: Props) {
  const [status] = useState(initial)
  const [step, setStep] = useState(initial.current_step)
  const [notice, setNotice] = useState<Notice>()
  const [busy, setBusy] = useState(false)
  const [system, setSystem] = useState<SystemStatus>()

  useEffect(() => {
    const saved = sessionStorage.getItem('rag_setup_step')
    if (saved && order.includes(saved as typeof order[number]) && initial.current_step !== 'administrator') setStep(saved as typeof step)
  }, [initial.current_step])

  async function advance(next: SetupStatus['current_step']) {
    setStep(next); sessionStorage.setItem('rag_setup_step', next)
    if (principal) await api.mutate('/v1/setup/progress', 'PUT', { step: next })
  }

  async function createAdministrator(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setNotice(undefined)
    const data = new FormData(event.currentTarget)
    const password = String(data.get('password')); const confirmation = String(data.get('confirmation'))
    if (password !== confirmation) { setBusy(false); setNotice({ kind: 'error', text: 'Пароли не совпадают.' }); return }
    try {
      const authenticated = await api.bootstrapSetup({
        tenant_slug: String(data.get('tenant_slug')), tenant_name: String(data.get('tenant_name')),
        username: String(data.get('username')), display_name: String(data.get('display_name')), password,
      }, String(data.get('setup_token') || '') || undefined)
      onAuthenticated(authenticated); setStep('models'); sessionStorage.setItem('rag_setup_step', 'models')
    } catch (reason) { setNotice({ kind: 'error', text: reason instanceof Error ? reason.message : 'Не удалось создать администратора.' }) }
    finally { setBusy(false) }
  }

  async function configureModels(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const form = event.currentTarget
    const data = new FormData(form)

    setBusy(true)
    setNotice(undefined)

    try {
        const roles: Array<'generation' | 'embedding' | 'reranker'> = [
            'generation',
            'embedding',
        ]

        if (data.get('reranker_model')) {
            roles.push('reranker')
        }

        for (const role of roles) {
            const token = String(data.get(`${role}_token`) || '')
            const credential = token
                ? `credential:setup-${role}`
                : null

            if (token) {
                await api.mutate(
                    `/v1/admin/credentials/${credential}`,
                    'PUT',
                    {
                        reference: credential,
                        kind: 'model_token',
                        secret: token,
                    },
                )
            }

            const created = await api.mutate<{ id: string }>(
                '/v1/admin/models',
                'POST',
                {
                    role,
                    model_id: data.get(`${role}_model`),
                    version: data.get(`${role}_version`),
                    base_url: data.get(`${role}_url`),
                    credential_ref: credential,
                    capabilities: {
                        provider: 'ollama',
                        ...(role === 'embedding'
                            ? {
                                  dimensions: Number(
                                      data.get('embedding_dimensions'),
                                  ),
                              }
                            : {}),
                    },
                },
            )

            const checked = await api.mutate<{
                status: 'ready' | 'model_loading' | 'unavailable'
            }>(
                `/v1/admin/models/${created.id}/test`,
                'POST',
            )

            if (checked.status === 'unavailable') {
                throw new Error(
                    `${role}: self-hosted endpoint недоступен.`,
                )
            }

            await api.mutate(
                `/v1/admin/models/${created.id}/activate`,
                'POST',
            )
        }

        form
            .querySelectorAll<HTMLInputElement>('input[type=password]')
            .forEach((input) => {
                input.value = ''
            })

        await advance('telegram')

        setNotice({
            kind: 'success',
            text: 'Модели сохранены и активированы.',
        })
    } catch (reason) {
        setNotice({
            kind: 'error',
            text:
                reason instanceof Error
                    ? reason.message
                    : 'Проверка модели не пройдена.',
        })
    } finally {
        setBusy(false)
    }
  }

  async function configureTelegram(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setNotice(undefined); const form = event.currentTarget; const data = new FormData(form)
    try {
      const enabled = data.get('enabled') === 'on'; let tokenRef: string | null = null; let keyRef: string | null = null
      if (enabled) {
        tokenRef = 'credential:telegram-bot'; keyRef = 'credential:telegram-api-key'
        await api.mutate(`/v1/admin/credentials/${tokenRef}`, 'PUT', { reference: tokenRef, kind: 'telegram_token', secret: data.get('bot_token') })
        const key = await api.mutate<{ api_key: string }>('/v1/api-keys', 'POST', { name: 'Telegram bot', scopes: ['inference:read', 'inference:write'] })
        await api.mutate(`/v1/admin/credentials/${keyRef}`, 'PUT', { reference: keyRef, kind: 'telegram_api_key', secret: key.api_key })
      }
      if (enabled && tokenRef && keyRef) {
        const bot = await api.mutate<{ id: string }>('/v1/admin/telegram-bots', 'POST', {
          name: 'Основной бот', enabled: true,
          token_credential_ref: tokenRef, api_key_credential_ref: keyRef,
        })
        await api.mutate(`/v1/admin/telegram-bots/${bot.id}/test`, 'POST')
      }
      form.reset(); await advance('readiness')
    } catch (reason) { setNotice({ kind: 'error', text: reason instanceof Error ? reason.message : 'Telegram не настроен.' }) }
    finally { setBusy(false) }
  }

  async function checkReadiness() {
    setBusy(true); setNotice(undefined)
    try { setSystem(await api.mutate<SystemStatus>('/v1/admin/system', 'GET')) }
    catch (reason) { setNotice({ kind: 'error', text: reason instanceof Error ? reason.message : 'Статусы недоступны.' }) }
    finally { setBusy(false) }
  }

  async function finish() {
    await api.mutate('/v1/setup/progress', 'PUT', { step: 'complete' })
    sessionStorage.removeItem('rag_setup_step'); onComplete()
  }

  return <main className="setup-shell"><section className="setup-card card">
    <header><div><p className="eyebrow">FIRST-RUN SETUP</p><h1>Настройка RAG Assistant</h1><p className="muted">Секреты шифруются до записи. Их значения не появятся повторно.</p></div><span className="step-count">Шаг {Math.max(1, order.indexOf(step as typeof order[number]) + 1)} / 4</span></header>
    <ol className="setup-progress">{order.map((item) => <li className={item === step ? 'active' : ''} key={item}>{item === 'administrator' ? 'Администратор' : item === 'models' ? 'Модели' : item === 'telegram' ? 'Telegram' : 'Готовность'}</li>)}</ol>
    {step === 'administrator' && <form className="form-grid" onSubmit={createAdministrator}><h2>Tenant и первый администратор</h2><label>Код tenant<input name="tenant_slug" defaultValue="local" pattern="[a-z0-9][a-z0-9-]*" required /></label><label>Название tenant<input name="tenant_name" defaultValue="Local workspace" required /></label><label>Логин администратора<input name="username" autoComplete="username" defaultValue="admin" required /></label><label>Имя администратора<input name="display_name" autoComplete="name" required /></label><label>Пароль<input name="password" type="password" minLength={12} autoComplete="new-password" required /></label><label>Повторите пароль<input name="confirmation" type="password" minLength={12} autoComplete="new-password" required /></label>{status.token_required && <label>Bootstrap token<input name="setup_token" type="password" required /></label>}<button disabled={busy}>{busy ? 'Создаём…' : 'Создать защищённое пространство'}</button></form>}
    {step === 'models' && <form className="form-grid" onSubmit={configureModels}><h2>Self-hosted модели</h2><fieldset><legend>Generation</legend><label>Endpoint<input name="generation_url" type="url" defaultValue="http://host.docker.internal:11434/v1" required /></label><label>Model ID<input name="generation_model" defaultValue="qwen2.5-coder:7b" required /></label><label>Version<input name="generation_version" defaultValue="1" required /></label><label>Bearer token (необязательно)<input name="generation_token" type="password" autoComplete="off" /></label></fieldset><fieldset><legend>Embedding</legend><label>Endpoint<input name="embedding_url" type="url" defaultValue="http://embedding-service:8001/v1" required /></label><label>Model ID<input name="embedding_model" defaultValue="BAAI/bge-m3" required /></label><label>Version<input name="embedding_version" defaultValue="bge-m3/1" required /></label><label>Dimensions<input name="embedding_dimensions" type="number" defaultValue="1024" min="1" required /></label></fieldset><fieldset><legend>Reranker (необязательно)</legend><label>Endpoint<input name="reranker_url" type="url" /></label><label>Model ID<input name="reranker_model" /></label><label>Version<input name="reranker_version" defaultValue="1" /></label><label>Bearer token<input name="reranker_token" type="password" autoComplete="off" /></label></fieldset><button disabled={busy}>{busy ? 'Проверяем…' : 'Сохранить и проверить модели'}</button></form>}
    {step === 'telegram' && <form className="form-grid" onSubmit={configureTelegram}><h2>Telegram</h2><label className="check"><input name="enabled" type="checkbox" /> Включить Telegram-бот</label><label>Bot token<input name="bot_token" type="password" autoComplete="off" /></label><p className="muted">Можно пропустить: бот останется в idle state и включится после настройки в Admin UI.</p><button disabled={busy}>{busy ? 'Сохраняем…' : 'Продолжить'}</button></form>}
    {step === 'readiness' && <div><h2>Итоговая проверка</h2><p>Cold start BGE-M3 может занять несколько минут. Состояние <strong>model_loading</strong> безопасно: повторите проверку позже.</p><button onClick={() => void checkReadiness()} disabled={busy}>Проверить сервисы</button>{system && <div className="status-grid">{Object.entries(system.components).map(([name, value]) => <article key={name}><strong>{name}</strong><span className={`status ${value}`}>{value}</span></article>)}</div>}<button className="secondary" onClick={() => void finish()}>Завершить setup и открыть продукт</button></div>}
    {notice && <p className={notice.kind === 'error' ? 'error' : 'success'} role="status">{notice.text}</p>}
  </section></main>
}
