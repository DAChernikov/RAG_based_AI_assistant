import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { App } from './App'

beforeEach(() => {
  Object.defineProperty(document, 'cookie', { writable: true, value: '' })
  vi.stubGlobal('fetch', vi.fn())
})
afterEach(() => cleanup())

test('shows the accessible login form without a browser session', async () => {
  vi.mocked(fetch).mockImplementation((input) => String(input).endsWith('/v1/setup/status') ? json({ required: false, setup_available: false, token_required: false, current_step: 'complete', onboarding_complete: true, config_version: 1 }) : json({}, 401))
  render(<App />)
  expect(await screen.findByRole('heading', { name: 'RAG Assistant' })).toBeInTheDocument()
  expect(screen.getByLabelText('Логин')).toHaveAttribute('autocomplete', 'username')
})

function json(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }))
}

test('admin manages typed users and sees an API key only once', async () => {
  const requests: Array<{ path: string; init?: RequestInit }> = []
  vi.mocked(fetch).mockImplementation((input, init) => {
    const path = String(input); requests.push({ path, init })
    if (path.endsWith('/v1/auth/login')) return json({ access_token: 'access' })
    if (path.endsWith('/v1/auth/me')) return json({ user_id: 'u1', tenant_id: 't1', username: 'admin', role: 'admin' })
    if (path.endsWith('/v1/knowledge-bases') || path.endsWith('/v1/conversations')) return json([])
    if (path.endsWith('/v1/admin/users') && init?.method === 'POST') return json({ id: 'u2' }, 201)
    if (path.endsWith('/v1/admin/users')) return json([])
    if (path.endsWith('/v1/api-keys') && init?.method === 'POST') return json({ id: 'k1', name: 'telegram', prefix: 'rag_live', scopes: ['inference:write'], api_key: 'rag_live_once-secret' }, 201)
    if (path.endsWith('/v1/api-keys')) return json([])
    return json([])
  })
  render(<App />)
  fireEvent.change(await screen.findByLabelText('Tenant'), { target: { value: 'acme' } })
  fireEvent.change(screen.getByLabelText('Логин'), { target: { value: 'admin' } })
  fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'a-long-password' } })
  fireEvent.click(screen.getByRole('button', { name: 'Войти' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Администрирование' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Доступ' }))
  expect(await screen.findByRole('heading', { name: 'Пользователи и роли' })).toBeVisible()
  fireEvent.change(screen.getByLabelText('Название'), { target: { value: 'telegram' } })
  fireEvent.change(screen.getByLabelText('Scopes через запятую'), { target: { value: 'inference:write' } })
  fireEvent.click(screen.getByRole('button', { name: 'Создать ключ' }))
  expect(await screen.findByText('rag_live_once-secret')).toBeVisible()
  expect(requests.some((request) => request.path.endsWith('/v1/api-keys') && request.init?.method === 'POST')).toBe(true)
})

test('source wizard sends a typed website contract instead of arbitrary JSON', async () => {
  let sourceBody = ''
  vi.mocked(fetch).mockImplementation((input, init) => {
    const path = String(input)
    if (path.endsWith('/v1/auth/login')) return json({ access_token: 'access' })
    if (path.endsWith('/v1/auth/me')) return json({ user_id: 'u1', tenant_id: 't1', username: 'admin', role: 'admin' })
    if (path.endsWith('/v1/admin/knowledge-sources') && init?.method === 'POST') { sourceBody = String(init.body); return json({ id: 's1' }, 201) }
    return json([])
  })
  render(<App />)
  fireEvent.change(await screen.findByLabelText('Tenant'), { target: { value: 'acme' } }); fireEvent.change(screen.getByLabelText('Логин'), { target: { value: 'admin' } }); fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'a-long-password' } }); fireEvent.click(screen.getByRole('button', { name: 'Войти' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Администрирование' })); fireEvent.click(screen.getByRole('button', { name: 'Подключения' }))
  fireEvent.change((await screen.findAllByLabelText('Название'))[1], { target: { value: 'Docs' } }); fireEvent.change(screen.getByLabelText('Root URL'), { target: { value: 'https://docs.example.org' } }); fireEvent.change(screen.getByLabelText('Разрешённые hosts'), { target: { value: 'docs.example.org' } }); fireEvent.click(screen.getByRole('button', { name: 'Добавить источник' }))
  await waitFor(() => expect(sourceBody).toContain('"source_type":"website"'))
  expect(sourceBody).toContain('"allowed_domains":["docs.example.org"]')
  expect(screen.queryByText('JSON contract')).not.toBeInTheDocument()
})

test('first run uses the setup wizard and never persists secret inputs', async () => {
  const requests: Array<{ path: string; body?: string }> = []
  vi.mocked(fetch).mockImplementation((input, init) => {
    const path = String(input); requests.push({ path, body: String(init?.body ?? '') })
    if (path.endsWith('/v1/setup/status')) return json({ required: true, setup_available: true, token_required: false, current_step: 'administrator', onboarding_complete: false, config_version: 1 })
    if (path.endsWith('/v1/setup/bootstrap')) return json({ access_token: 'setup-access' }, 201)
    if (path.endsWith('/v1/auth/me')) return json({ user_id: 'u1', tenant_id: 't1', username: 'admin', role: 'admin' })
    if (path.endsWith('/v1/admin/models')) return json({ id: `model-${requests.length}` }, 201)
    return json({ status: 'ready' })
  })
  render(<App />)
  expect(await screen.findByRole('heading', { name: 'Настройка RAG Assistant' })).toBeVisible()
  fireEvent.change(screen.getByLabelText('Имя администратора'), { target: { value: 'Owner' } })
  fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'owner-password-123' } })
  fireEvent.change(screen.getByLabelText('Повторите пароль'), { target: { value: 'owner-password-123' } })
  fireEvent.click(screen.getByRole('button', { name: 'Создать защищённое пространство' }))
  expect(await screen.findByRole('heading', { name: 'Self-hosted модели' })).toBeVisible()
  expect(sessionStorage.getItem('rag_setup_step')).toBe('models')
  expect(sessionStorage.getItem('rag_setup_step')).not.toContain('owner-password-123')
  expect(requests.some((request) => request.path.endsWith('/v1/setup/bootstrap'))).toBe(true)
})
