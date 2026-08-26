import { useState } from 'react'

const userSteps = [
  ['Выберите режим ответа', '«Без базы знаний» обращается к активной модели напрямую. База знаний добавляет проверяемый контекст и ссылки на источники.'],
  ['Задайте конкретный вопрос', 'Опишите результат, формат и ограничения. Для SQL укажите нужные таблицы или бизнес-термины.'],
  ['Проверьте источники', 'У grounded-ответа раскройте «Источники»: там показаны URI, тип источника и score. У model-only ответа ссылок нет.'],
  ['Продолжите диалог', 'История сохраняется автоматически. Оценка «Полезно / Не помогло» помогает контролировать качество.'],
]

const adminSteps = [
  ['Проверьте систему', 'Откройте «Система» и дождитесь готовности PostgreSQL, Redis, workers, generation и embedding endpoints.'],
  ['Подключите модели', 'В «Моделях» выберите установленную Ollama-модель или добавьте self-hosted OpenAI-compatible endpoint, проверьте соединение и активируйте конфигурацию.'],
  ['Создайте подключения', 'В «Подключениях» добавьте Website, Git или PostgreSQL metadata source. Credentials вводятся в форме подключения и сохраняются зашифрованно.'],
  ['Соберите базу знаний', 'Создайте базу и отметьте любое число доступных источников. Один источник может входить в несколько баз.'],
  ['Запустите синхронизацию', 'Refresh получает содержимое источников. После успешного ingestion запустите индексирование из карточки базы знаний.'],
  ['Настройте доступ', 'Создайте пользователей и scoped API keys. Ключ показывается полностью только один раз.'],
  ['Автоматизируйте', 'Настройте расписания refresh и retention dry-run. Перед удалением всегда проверяйте список кандидатов.'],
]

export function Guide({ isAdmin }: { isAdmin: boolean }) {
  const [section, setSection] = useState<'user' | 'admin'>('user')
  const steps = section === 'user' ? userSteps : adminSteps
  return <section className="workspace guide-page">
    <header className="page-header"><div><p className="eyebrow">PRODUCT GUIDE</p><h1>Как пользоваться ассистентом</h1><p className="lead">Короткие практические маршруты — от первого вопроса до собственной базы знаний.</p></div></header>
    {isAdmin && <div className="segmented" role="tablist" aria-label="Раздел руководства"><button className={section === 'user' ? 'active' : 'secondary'} onClick={() => setSection('user')}>Для пользователя</button><button className={section === 'admin' ? 'active' : 'secondary'} onClick={() => setSection('admin')}>Для администратора</button></div>}
    <div className="guide-layout">
      <ol className="step-list">{steps.map(([title, description], index) => <li key={title}><span className="step-number">{index + 1}</span><div><h3>{title}</h3><p>{description}</p></div></li>)}</ol>
      <aside className="card guide-note"><h3>Как выбрать режим</h3><dl><dt>Без базы знаний</dt><dd>Общие вопросы и знания самой модели. Ответ может быть устаревшим, citations не создаются.</dd><dt>База знаний</dt><dd>Вопросы по вашей документации, коду и схеме БД. Ответ опирается на активный индекс.</dd><dt>SQL</dt><dd>Router сам включает безопасную AST/schema validation для SQL-вопросов.</dd></dl></aside>
    </div>
  </section>
}
