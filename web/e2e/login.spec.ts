import { expect, test } from '@playwright/test'

test('login shell is keyboard accessible', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'RAG Assistant' })).toBeVisible()
  await page.keyboard.press('Tab')
  await expect(page.locator('input[name=tenant]')).toBeFocused()
})
