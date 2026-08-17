import { expect, test } from '@playwright/test'

test('login shell is keyboard accessible', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'RAG Assistant' })).toBeVisible()
  await page.keyboard.press('Tab')
  await expect(page.locator('input[name=tenant]')).toBeFocused()
})

test('owner flow logs in, administers a typed source, streams a cited answer and logs out', async ({ page }) => {
  const posted: string[] = []
  await page.route('**/*', async (route) => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname
    if (!path.startsWith('/v1') && path !== '/ask/stream') return route.continue()
    posted.push(`${request.method()} ${path}`)
    const fulfill = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/v1/auth/login') return fulfill({ access_token: 'access' })
    if (path === '/v1/auth/me') return fulfill({ user_id: 'u1', tenant_id: 't1', username: 'owner', role: 'admin' })
    if (path === '/v1/knowledge-bases' || path === '/v1/admin/knowledge-bases') return fulfill([{ id: 'kb1', name: 'Product', is_enabled: true }])
    if (path === '/v1/conversations') return fulfill([])
    if (path === '/v1/admin/knowledge-sources' && request.method() === 'POST') return fulfill({ id: 'source1' }, 201)
    if (path === '/v1/admin/knowledge-sources') return fulfill([])
    if (path === '/v1/admin/users' || path === '/v1/api-keys') return fulfill([])
    if (path === '/ask/stream') return route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'id: 1\ndata: {"type":"token","data":"Grounded answer","job_id":"job1"}\n\nid: 2\ndata: {"type":"meta","data":{"retrieved":[{"doc_id":"chunk1","source":"website","title":"Guide","uri":"https://docs.local/guide","score":0.9}]},"job_id":"job1"}\n\nid: 3\ndata: {"type":"completed","data":{},"job_id":"job1"}\n\n' })
    if (path === '/v1/inference-jobs/job1') return fulfill({ job_id: 'job1', conversation_id: 'c1', status: 'completed', answer: { answer_id: 'a1' } })
    if (path === '/v1/auth/logout') return route.fulfill({ status: 204, body: '' })
    return fulfill([])
  })
  await page.goto('/')
  await page.getByLabel('Tenant').fill('acme'); await page.getByLabel('Логин').fill('owner'); await page.getByLabel('Пароль').fill('owner-password'); await page.getByRole('button', { name: 'Войти' }).click()
  await expect(page.getByRole('heading', { name: 'Ассистент' })).toBeVisible()
  await page.getByRole('button', { name: 'Admin' }).click(); await page.getByRole('button', { name: 'Источники' }).click()
  await page.locator('form').filter({ has: page.getByLabel('Root URL') }).locator('input[name=name]').fill('Local docs'); await page.getByLabel('Root URL').fill('https://docs.local'); await page.getByLabel('Разрешённые hosts').fill('docs.local'); await page.getByRole('button', { name: 'Добавить источник' }).click()
  expect(posted).toContain('POST /v1/admin/knowledge-sources')
  await page.getByRole('button', { name: 'Чат' }).click(); await page.getByLabel('Вопрос').fill('How does the product work?'); await page.getByRole('button', { name: 'Отправить' }).click()
  await expect(page.getByText('Grounded answer')).toBeVisible(); await expect(page.getByText('Источники (1)')).toBeVisible()
  await page.getByRole('button', { name: 'Выйти' }).click(); await expect(page.getByRole('heading', { name: 'RAG Assistant' })).toBeVisible()
})
