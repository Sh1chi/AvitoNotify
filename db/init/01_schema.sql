/* ========================== 0. СХЕМА ========================== */
CREATE SCHEMA IF NOT EXISTS notify;
SET search_path TO notify;

/* ======================= 1. АККАУНТЫ ========================= */
/* Avito-продавцы/аккаунты (id в нашей БД и avito_user_id из Авито) */
CREATE TABLE IF NOT EXISTS accounts (
    id              SERIAL  PRIMARY KEY,
    avito_user_id   BIGINT  UNIQUE NOT NULL,  -- как в URL/объектах Avito
    name            TEXT,                     -- произвольный alias сотрудника/аккаунта
    display_name    TEXT,                     -- кастомное отображаемое имя
    created_ts      TIMESTAMPTZ NOT NULL DEFAULT now()
);

/* ==================== 2. TELEGRAM ОБЪЕКТЫ ==================== */
/* 2.1 Боты Telegram (опционально — если бота один, будет одна запись) */
CREATE TABLE IF NOT EXISTS telegram_bots (
    id              SERIAL PRIMARY KEY,
    tg_bot_id       BIGINT UNIQUE,            -- числовой ID бота (не токен)
    username        TEXT UNIQUE,              -- @username
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_ts      TIMESTAMPTZ NOT NULL DEFAULT now()
);

/* 2.2 Чаты/группы/каналы Telegram */
CREATE TABLE IF NOT EXISTS telegram_chats (
    id              SERIAL PRIMARY KEY,
    tg_chat_id      BIGINT NOT NULL UNIQUE,   -- chat_id из Telegram
    type            TEXT  NOT NULL CHECK (type IN ('group','supergroup','channel','private')),
    title           TEXT,
    created_ts      TIMESTAMPTZ NOT NULL DEFAULT now()
);

/* ================= 3. СВЯЗКИ АККАУНТ ↔ TG ГРУППА ============== */
/* Здесь же — настройки уведомлений на уровне конкретной пары */
CREATE TABLE IF NOT EXISTS account_chat_links (
    account_id          INT NOT NULL
                        REFERENCES accounts(id) ON DELETE CASCADE,
    chat_id             INT NOT NULL
                        REFERENCES telegram_chats(id) ON DELETE CASCADE,
    bot_id              INT
                        REFERENCES telegram_bots(id) ON DELETE SET NULL,

    muted               BOOLEAN NOT NULL DEFAULT FALSE,       -- «кнопка выключить бота»
    work_from           TIME    NOT NULL DEFAULT TIME '09:00',
    work_to             TIME    NOT NULL DEFAULT TIME '21:00',
    tz                  TEXT    NOT NULL DEFAULT 'UTC',       -- пример: 'Europe/Moscow'

    daily_digest_time   TIME,                                 -- если задано — шлём утренний отчёт
    last_digest_ts      TIMESTAMPTZ,                          -- когда в последний раз отправляли

    created_ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_ts          TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (account_id, chat_id)
);

/* Индексы для быстрых выборок */
CREATE INDEX IF NOT EXISTS account_chat_links_acc_idx
    ON account_chat_links (account_id)
    WHERE muted = FALSE;

CREATE INDEX IF NOT EXISTS account_chat_links_chat_idx
    ON account_chat_links (chat_id);

/* Триггер авто-обновления updated_ts */
CREATE OR REPLACE FUNCTION set_updated_ts()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_ts := now();
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_account_chat_links_updated_ts ON account_chat_links;
CREATE TRIGGER trg_account_chat_links_updated_ts
BEFORE UPDATE ON account_chat_links
FOR EACH ROW EXECUTE FUNCTION set_updated_ts();

/* ===================== 4. НАПОМИНАНИЯ (MVP) =================== */
/* Привязаны к чату Авито (строковый chat_id по контракту Авито) */
CREATE TABLE IF NOT EXISTS reminders (
    account_id      INT  NOT NULL
                    REFERENCES accounts(id) ON DELETE CASCADE,
    avito_chat_id   TEXT NOT NULL,                 -- string из API Авито
    avito_chat_title  TEXT,
    first_ts        TIMESTAMPTZ NOT NULL,          -- когда впервые заметили «без ответа»
    last_reminder   TIMESTAMPTZ,                   -- когда в последний раз напомнили
    PRIMARY KEY (account_id, avito_chat_id)
);

/* Популярные индексы */
CREATE INDEX IF NOT EXISTS reminders_due_idx
    ON reminders (account_id, last_reminder);

CREATE INDEX IF NOT EXISTS reminders_acc_chat_idx
    ON reminders (account_id, avito_chat_id);

/* ====================== 5. ПОЛЕЗНЫЕ ПРЕДСТАВЛЕНИЯ ============= */
/* Удобный просмотр: куда слать уведомления от аккаунта */
CREATE OR REPLACE VIEW notify.v_account_chat_targets AS
SELECT
    a.id                          AS account_id,
    a.avito_user_id,
    COALESCE(a.display_name, a.name) AS account_name,
    l.muted,
    l.work_from, l.work_to, l.tz,
    l.daily_digest_time, l.last_digest_ts,
    tc.tg_chat_id,
    tc.type       AS tg_chat_type,
    tc.title      AS tg_chat_title,
    tb.username   AS bot_username,
    tb.is_active  AS bot_active
FROM notify.account_chat_links l
JOIN notify.accounts a         ON a.id = l.account_id
JOIN notify.telegram_chats tc  ON tc.id = l.chat_id
LEFT JOIN notify.telegram_bots tb ON tb.id = l.bot_id;
