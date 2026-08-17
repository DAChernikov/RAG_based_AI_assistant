import { render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from './App'

beforeEach(() => {
  Object.defineProperty(document, 'cookie', { writable: true, value: '' })
  vi.stubGlobal('fetch', vi.fn())
})

test('shows the accessible login form without a browser session', async () => {
  render(<App />)
  expect(await screen.findByRole('heading', { name: 'RAG Assistant' })).toBeInTheDocument()
  expect(screen.getByLabelText('Логин')).toHaveAttribute('autocomplete', 'username')
})
