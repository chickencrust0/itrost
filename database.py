"""
database.py — локальное хранилище (SQLite): пользователи, файлы ДЗ,
заявки на перенос, лог напоминаний.

Портировано из alfacrm-bot без структурных изменений — только
telegram_id переименован в max_user_id, т.к. идентификатор теперь
приходит от MAX. Комментарии о причинах решений (WAL, чанки IN(...),
дедупликация напоминаний, миграция author_role) сохранены из
оригинала — это по-прежнему актуальные грабли, а не байки.
"""

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# SQLite ограничивает число параметров в запросе (обычно 999).
_SQL_VAR_CHUNK = 500


class Database:
    def __init__(self, db_path: str = "bot.db"):
        self.db_path = db_path
        self._init_db()

    # ==================== СОЕДИНЕНИЯ ====================

    @contextmanager
    def _conn(self):
        """
        `with sqlite3.connect(...)` фиксирует транзакцию, но НЕ закрывает
        соединение — незакрытое соединение течёт дескрипторами на каждом
        запросе, поэтому закрываем явно в finally.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    max_user_id INTEGER PRIMARY KEY,
                    crm_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('teacher', 'parent', 'manager')),
                    phone TEXT NOT NULL,
                    full_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS notification_preferences (
                    max_user_id INTEGER PRIMARY KEY,
                    lessons INTEGER NOT NULL DEFAULT 1,
                    summaries INTEGER NOT NULL DEFAULT 1,
                    updates INTEGER NOT NULL DEFAULT 1,
                    broadcasts INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS homework_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_name TEXT,
                    file_type TEXT DEFAULT 'document',
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS transfer_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_max_id INTEGER NOT NULL,
                    lesson_id TEXT NOT NULL DEFAULT '',
                    comment TEXT,
                    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    resolved_by INTEGER,
                    FOREIGN KEY (teacher_max_id) REFERENCES users(max_user_id)
                );

                CREATE TABLE IF NOT EXISTS reminder_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id TEXT NOT NULL,
                    reminder_type TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    target_max_id INTEGER,
                    status TEXT DEFAULT 'sent'
                );

                -- Тема, ДЗ и отметка «проведён» хранятся здесь, а не в CRM:
                -- у сущности schedule в AlfaCRM таких полей нет вообще
                -- (подтверждено снятой схемой аккаунта). Раньше бот пытался
                -- писать их в CRM и молча ничего не сохранял, поэтому у
                -- родителя раздел ДЗ всегда был пустым.
                CREATE TABLE IF NOT EXISTS lesson_notes (
                    lesson_id TEXT PRIMARY KEY,
                    topic TEXT,
                    homework TEXT,
                    status INTEGER,
                    updated_by INTEGER,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                -- Заявки от новых посетителей (ещё не клиентов CRM).
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    max_user_id INTEGER NOT NULL,
                    full_name TEXT,
                    phone TEXT,
                    ages TEXT,
                    direction TEXT,
                    source TEXT DEFAULT 'max',
                    -- draft: человек ещё в диалоге, номер не оставил.
                    -- Черновик создаётся сразу, чтобы не потерять обращение,
                    -- если посетитель уйдёт на середине.
                    status TEXT DEFAULT 'new'
                        CHECK(status IN ('draft', 'new', 'in_progress', 'done', 'rejected')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    resolved_by INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status, created_at);
                -- Персональное домашнее задание.
                --
                -- Раньше ДЗ было ОДНО на занятие (lesson_notes.homework),
                -- но в группе дети идут разными темпами, и педагогу
                -- нужно давать разное. client_id = 0 означает «всем в
                -- группе»; персональное ДЗ имеет приоритет над общим.
                CREATE TABLE IF NOT EXISTS lesson_homework (
                    lesson_id TEXT NOT NULL,
                    client_id INTEGER NOT NULL DEFAULT 0,
                    homework TEXT,
                    updated_by INTEGER,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (lesson_id, client_id)
                );

                CREATE INDEX IF NOT EXISTS idx_homework_lesson ON homework_files(lesson_id);
                CREATE INDEX IF NOT EXISTS idx_lesson_homework
                    ON lesson_homework(lesson_id, client_id);
                CREATE INDEX IF NOT EXISTS idx_transfer_status ON transfer_requests(status);
                CREATE INDEX IF NOT EXISTS idx_reminder_lookup
                    ON reminder_log(lesson_id, reminder_type, target_max_id, sent_at);

                -- Неявки. В AlfaCRM нет статуса «не пришёл» до момента
                -- списания занятия, поэтому отметка преподавателя живёт
                -- здесь, а в CRM уходит только решение менеджера (burn_one).
                -- lesson_id — TEXT, потому что у повторяющихся занятий id
                -- синтетический вида "116:2026-09-01".
                CREATE TABLE IF NOT EXISTS absences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id TEXT NOT NULL,
                    client_id INTEGER NOT NULL,
                    client_name TEXT,
                    lesson_date TEXT NOT NULL,
                    teacher_max_id INTEGER,
                    status TEXT DEFAULT 'pending'
                        CHECK(status IN ('pending', 'burned', 'excused')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    resolved_by INTEGER,
                    UNIQUE(lesson_id, client_id)
                );

                CREATE INDEX IF NOT EXISTS idx_absences_date
                    ON absences(lesson_date, status);

                -- Обращения в поддержку: диалог «клиент <-> менеджер».
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_max_id INTEGER NOT NULL,
                    user_name TEXT,
                    user_role TEXT,
                    user_phone TEXT,
                    status TEXT DEFAULT 'open' CHECK(status IN ('open', 'closed')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    closed_by INTEGER
                );

                CREATE TABLE IF NOT EXISTS support_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    sender_max_id INTEGER,
                    sender_side TEXT CHECK(sender_side IN ('user', 'manager')),
                    text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ticket_id) REFERENCES support_tickets(id)
                );

                CREATE INDEX IF NOT EXISTS idx_tickets_status
                    ON support_tickets(status, user_max_id);
                CREATE INDEX IF NOT EXISTS idx_ticket_messages
                    ON support_messages(ticket_id, created_at);

                -- Заморозки занятий. В AlfaCRM отдельной сущности для
                -- этого нет: списание идёт через burn_one, а счётчик
                -- беспричинных заморозок хранится в поле email клиента
                -- (см. alfacrm_client.get_free_freezes). Здесь ведём
                -- журнал: кто, когда, по какой причине и со справкой ли.
                -- Остаток БЕСПРИЧИННЫХ заморозок, когда его негде
                -- хранить в CRM. Используется, только если в AlfaCRM не
                -- заведено дополнительное поле ALFACRM_FIELD_FREEZES —
                -- тогда источником становится CRM, а эта таблица
                -- простаивает (см. freezes.FreezeCounter).
                CREATE TABLE IF NOT EXISTS client_freezes (
                    client_id INTEGER PRIMARY KEY,
                    free_left INTEGER NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS freezes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER NOT NULL,
                    client_name TEXT,
                    lesson_id TEXT,
                    lesson_date TEXT,
                    kind TEXT NOT NULL CHECK(kind IN ('no_reason', 'valid')),
                    reason TEXT,
                    created_by INTEGER,
                    created_by_role TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    certificate_file_id TEXT,
                    certificate_type TEXT,
                    certificate_at TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_freezes_client
                    ON freezes(client_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_freezes_kind
                    ON freezes(kind, certificate_file_id);

                -- Журнал активности в боте. Раньше эти события уходили
                -- менеджеру сообщениями и топили в себе всё остальное —
                -- заявки, обращения, решения по неявкам. Теперь они
                -- копятся здесь и показываются только по кнопке
                -- «Активность в боте» в меню менеджера.
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    max_user_id INTEGER NOT NULL,
                    user_name TEXT,
                    event TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_activity_time
                    ON activity_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_activity_user
                    ON activity_log(max_user_id, created_at);
            """)

            columns = {row["name"] for row in conn.execute("PRAGMA table_info(transfer_requests)")}
            if "author_role" not in columns:
                conn.execute(
                    "ALTER TABLE transfer_requests ADD COLUMN author_role TEXT DEFAULT 'teacher'"
                )
                logger.info("🛠 transfer_requests: добавлена колонка author_role")

            # Источник, из которого пришёл пользователь (max / ig_follow / ...).
            user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
            if "source" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN source TEXT DEFAULT 'max'")
                logger.info("🛠 users: добавлена колонка source")

            # Файлы ДЗ теперь тоже адресные: 0 = всей группе.
            hw_columns = {row["name"] for row in conn.execute("PRAGMA table_info(homework_files)")}
            if "client_id" not in hw_columns:
                conn.execute(
                    "ALTER TABLE homework_files ADD COLUMN client_id INTEGER NOT NULL DEFAULT 0"
                )
                logger.info("🛠 homework_files: добавлена колонка client_id")

            # Ранее выданное общее ДЗ переносим в новую таблицу как
            # групповое (client_id = 0), иначе родители перестали бы
            # видеть уже выданные задания сразу после обновления.
            moved = conn.execute("""
                INSERT OR IGNORE INTO lesson_homework (lesson_id, client_id, homework, updated_by)
                SELECT lesson_id, 0, homework, updated_by FROM lesson_notes
                WHERE homework IS NOT NULL AND TRIM(homework) <> ''
            """).rowcount
            if moved:
                logger.info(f"🛠 Перенесено общих ДЗ в lesson_homework: {moved}")

    # ==================== ПОЛЬЗОВАТЕЛИ ====================

    NOTIFICATION_CATEGORIES = ("lessons", "summaries", "updates", "broadcasts")

    def get_notification_preferences(self, max_user_id: int) -> Dict[str, bool]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT lessons, summaries, updates, broadcasts "
                "FROM notification_preferences WHERE max_user_id = ?", (max_user_id,)
            ).fetchone()
        return {key: bool(row[key]) if row else True for key in self.NOTIFICATION_CATEGORIES}

    def notification_enabled(self, max_user_id: int, category: str) -> bool:
        if category not in self.NOTIFICATION_CATEGORIES:
            raise ValueError(f"Unknown notification category: {category}")
        return self.get_notification_preferences(max_user_id)[category]

    def toggle_notification(self, max_user_id: int, category: str) -> Dict[str, bool]:
        if category not in self.NOTIFICATION_CATEGORIES:
            raise ValueError(f"Unknown notification category: {category}")
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO notification_preferences (max_user_id) VALUES (?)", (max_user_id,))
            conn.execute(
                f"UPDATE notification_preferences SET {category} = 1 - {category} "
                "WHERE max_user_id = ?", (max_user_id,)
            )
        return self.get_notification_preferences(max_user_id)

    def deactivate_user(self, max_user_id: int):
        with self._conn() as conn:
            conn.execute("UPDATE users SET is_active = 0 WHERE max_user_id = ?", (max_user_id,))

    def link_user(
        self,
        max_user_id: int,
        crm_id: int,
        role: str,
        phone: str,
        full_name: str,
        source: str = "max",
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO users (max_user_id, crm_id, role, phone, full_name, source, is_active)
                   VALUES (?,?,?,?,?,?,1)
                   ON CONFLICT(max_user_id) DO UPDATE SET
                       crm_id=excluded.crm_id,
                       role=excluded.role,
                       phone=excluded.phone,
                       full_name=excluded.full_name,
                       is_active=1""",
                (max_user_id, crm_id, role, phone, full_name, source),
            )

    def get_user(self, max_user_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE max_user_id = ? AND is_active = 1", (max_user_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_crm_id(self, crm_id: int, role: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE crm_id = ? AND role = ? AND is_active = 1",
                (crm_id, role),
            ).fetchone()
            return dict(row) if row else None

    def get_manager_ids(self) -> List[int]:
        """MAX id менеджеров, вошедших по паролю (/manager)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT max_user_id FROM users WHERE role='manager' AND is_active=1"
            ).fetchall()
            return [row["max_user_id"] for row in rows]

    def get_all_users_by_role(self, role: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE role = ? AND is_active = 1", (role,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_login_report(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Кто вошёл в бота, а кто нет.

        «Вошёл» = есть активная запись в users, то есть человек прошёл
        вход по номеру и найден в CRM. «Не вошёл» — те, кто оставил
        заявку, но входа не проходил (таблица leads), плюс те, кого
        деактивировали кнопкой «Выйти из профиля»: для менеджера это
        ровно те люди, до которых бот сейчас не достучится.
        """
        with self._conn() as conn:
            active = [
                dict(r) for r in conn.execute(
                    "SELECT max_user_id, full_name, role, phone, source "
                    "FROM users WHERE is_active = 1 ORDER BY role, full_name"
                ).fetchall()
            ]
            inactive = [
                dict(r) for r in conn.execute(
                    "SELECT max_user_id, full_name, role, phone, source "
                    "FROM users WHERE is_active = 0 ORDER BY role, full_name"
                ).fetchall()
            ]
            leads = [
                dict(r) for r in conn.execute(
                    """SELECT max_user_id, MAX(full_name) AS full_name,
                              MAX(phone) AS phone
                       FROM leads
                       WHERE max_user_id NOT IN (SELECT max_user_id FROM users)
                       GROUP BY max_user_id
                       ORDER BY MAX(created_at) DESC"""
                ).fetchall()
            ]
            return {"active": active, "inactive": inactive, "leads": leads}

    # ==================== ДОМАШНИЕ ЗАДАНИЯ ====================

    def add_homework_file(
        self, lesson_id: Any, file_id: str, file_name: str,
        file_type: str = "document", client_id: int = 0,
    ):
        """client_id = 0 — файл для всей группы."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO homework_files "
                "(lesson_id, file_id, file_name, file_type, client_id) VALUES (?,?,?,?,?)",
                (str(lesson_id), file_id, file_name, file_type, int(client_id or 0)),
            )

    def get_homework_files(
        self, lesson_id: Any, client_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Файлы ДЗ по занятию.

        client_id=None — все файлы (взгляд преподавателя). С client_id
        возвращаются файлы этого ученика И общие для группы (client_id=0):
        родитель должен видеть и то, и другое.
        """
        with self._conn() as conn:
            if client_id is None:
                rows = conn.execute(
                    "SELECT * FROM homework_files WHERE lesson_id = ? "
                    "ORDER BY uploaded_at DESC",
                    (str(lesson_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM homework_files WHERE lesson_id = ? "
                    "AND client_id IN (0, ?) ORDER BY uploaded_at DESC",
                    (str(lesson_id), int(client_id)),
                ).fetchall()
            return [dict(row) for row in rows]

    # ==================== ПЕРСОНАЛЬНОЕ ДЗ ====================
    #
    # client_id = 0 означает «всем в группе». Персональное задание
    # перекрывает групповое: если педагог дал ребёнку своё ДЗ, родитель
    # должен увидеть именно его, а не общее.

    HOMEWORK_ALL = 0

    def set_lesson_homework(
        self, lesson_id: Any, client_id: int, homework: str, updated_by: Optional[int] = None
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO lesson_homework (lesson_id, client_id, homework, updated_by)
                   VALUES (?,?,?,?)
                   ON CONFLICT(lesson_id, client_id) DO UPDATE SET
                       homework=excluded.homework,
                       updated_by=excluded.updated_by,
                       updated_at=CURRENT_TIMESTAMP""",
                (str(lesson_id), int(client_id), homework, updated_by),
            )

    def get_lesson_homework(self, lesson_id: Any, client_id: Optional[int] = None) -> str:
        """Текст ДЗ: сперва персональный, при его отсутствии — групповой."""
        with self._conn() as conn:
            if client_id is not None:
                row = conn.execute(
                    "SELECT homework FROM lesson_homework WHERE lesson_id=? AND client_id=?",
                    (str(lesson_id), int(client_id)),
                ).fetchone()
                if row and (row["homework"] or "").strip():
                    return row["homework"]
            row = conn.execute(
                "SELECT homework FROM lesson_homework WHERE lesson_id=? AND client_id=0",
                (str(lesson_id),),
            ).fetchone()
            return (row["homework"] if row else "") or ""

    def get_lesson_homework_targets(self, lesson_id: Any) -> Dict[int, str]:
        """{client_id: текст} по занятию — для сводки преподавателя."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT client_id, homework FROM lesson_homework WHERE lesson_id=?",
                (str(lesson_id),),
            ).fetchall()
            return {int(r["client_id"]): r["homework"] or "" for r in rows}

    def get_homework_file_counts(
        self, lesson_ids: Optional[Sequence[Any]] = None
    ) -> Dict[str, int]:
        """
        {lesson_id: количество файлов ДЗ} одним запросом.

        lesson_id — СТРОКА: у повторяющихся занятий AlfaCRM id
        синтетический, вида "116:2026-09-01" (см. alfacrm_client).
        Раньше здесь стоял int(i), и сводка менеджера падала на первом же
        таком занятии.
        """
        counts: Dict[str, int] = {}
        with self._conn() as conn:
            if lesson_ids is None:
                rows = conn.execute(
                    "SELECT lesson_id, COUNT(*) AS cnt FROM homework_files GROUP BY lesson_id"
                ).fetchall()
                return {str(row["lesson_id"]): row["cnt"] for row in rows}

            ids = [str(i) for i in lesson_ids if i is not None]
            for start in range(0, len(ids), _SQL_VAR_CHUNK):
                chunk = ids[start:start + _SQL_VAR_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT lesson_id, COUNT(*) AS cnt FROM homework_files "
                    f"WHERE lesson_id IN ({placeholders}) GROUP BY lesson_id",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    counts[str(row["lesson_id"])] = row["cnt"]
        return counts

    # ==================== ТЕМА / ДЗ / СТАТУС ЗАНЯТИЯ ====================
    #
    # У сущности schedule в AlfaCRM нет полей темы, домашнего задания и
    # статуса (подтверждено снятой схемой аккаунта, см. schema.md). Раньше
    # бот пытался писать их в CRM: запись молча не сохранялась, и родитель
    # всегда видел «Домашних заданий нет», а отчёт преподавателя показывал
    # ноль проведённых уроков. Поэтому эти три вещи живут здесь.

    def set_lesson_note(
        self,
        lesson_id: Any,
        *,
        topic: Optional[str] = None,
        homework: Optional[str] = None,
        status: Optional[int] = None,
        updated_by: Optional[int] = None,
    ) -> None:
        """Обновляет только переданные поля, остальные не трогает."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO lesson_notes (lesson_id) VALUES (?)",
                (str(lesson_id),),
            )
            sets, params = [], []
            for column, value in (
                ("topic", topic), ("homework", homework), ("status", status)
            ):
                if value is not None:
                    sets.append(f"{column} = ?")
                    params.append(value)
            if not sets:
                return
            sets.append("updated_by = ?")
            params.append(updated_by)
            sets.append("updated_at = CURRENT_TIMESTAMP")
            params.append(str(lesson_id))
            conn.execute(
                f"UPDATE lesson_notes SET {', '.join(sets)} WHERE lesson_id = ?",
                tuple(params),
            )

    def get_lesson_notes(
        self, lesson_ids: Optional[Sequence[Any]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """{lesson_id: {topic, homework, status}} одним запросом."""
        notes: Dict[str, Dict[str, Any]] = {}
        with self._conn() as conn:
            if lesson_ids is None:
                rows = conn.execute("SELECT * FROM lesson_notes").fetchall()
                return {str(r["lesson_id"]): dict(r) for r in rows}

            ids = [str(i) for i in lesson_ids if i is not None]
            for start in range(0, len(ids), _SQL_VAR_CHUNK):
                chunk = ids[start:start + _SQL_VAR_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT * FROM lesson_notes WHERE lesson_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                for r in rows:
                    notes[str(r["lesson_id"])] = dict(r)
        return notes

    def get_lesson_note(self, lesson_id: Any) -> Dict[str, Any]:
        return self.get_lesson_notes([lesson_id]).get(str(lesson_id), {})

    # ==================== ЗАЯВКИ ОТ НОВЫХ КЛИЕНТОВ ====================

    def upsert_draft_lead(
        self,
        max_user_id: int,
        *,
        ages: str = "",
        direction: str = "",
        full_name: str = "",
        phone: str = "",
    ) -> int:
        """
        Черновик заявки: создаётся, как только человек назвал возраст, и
        дополняется по мере выбора направлений.

        Зачем: раньше в базе не сохранялось НИЧЕГО до момента, когда
        посетитель пришлёт телефон. Кто-то дошёл до выбора направления и
        передумал оставлять номер — обращение исчезало бесследно, и
        менеджер о нём не узнавал. Теперь такие обращения видны как
        незавершённые.

        Черновик у пользователя один: пока он не оставил номер, повторные
        шаги обновляют ту же строку, а не плодят новые.
        """
        with self._conn() as conn:
            row = conn.execute(
                """SELECT id FROM leads
                   WHERE max_user_id = ? AND status = 'draft'
                   ORDER BY created_at DESC LIMIT 1""",
                (max_user_id,),
            ).fetchone()

            if row:
                sets, params = [], []
                for column, value in (
                    ("ages", ages), ("direction", direction),
                    ("full_name", full_name), ("phone", phone),
                ):
                    if value:
                        sets.append(f"{column} = ?")
                        params.append(value)
                if sets:
                    params.append(row["id"])
                    conn.execute(
                        f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", tuple(params)
                    )
                return row["id"]

            cursor = conn.execute(
                """INSERT INTO leads
                       (max_user_id, full_name, phone, ages, direction, status)
                   VALUES (?,?,?,?,?,'draft')""",
                (max_user_id, full_name, phone, ages, direction),
            )
            return cursor.lastrowid

    def promote_draft_lead(
        self, max_user_id: int, *, phone: str, ages: str, direction: str
    ) -> Optional[int]:
        """Черновик → полноценная заявка. None, если черновика нет."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT id FROM leads
                   WHERE max_user_id = ? AND status = 'draft'
                   ORDER BY created_at DESC LIMIT 1""",
                (max_user_id,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """UPDATE leads
                   SET phone=?, ages=?, direction=?, status='new',
                       created_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (phone, ages, direction, row["id"]),
            )
            return row["id"]

    def get_draft_leads(self, max_age_hours: int = 720) -> List[Dict[str, Any]]:
        """Незавершённые обращения: дошли до выбора, но номер не оставили."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM leads
                   WHERE status = 'draft'
                     AND created_at >= datetime('now', ?)
                   ORDER BY created_at DESC""",
                (f"-{max_age_hours} hours",),
            ).fetchall()
            return [dict(r) for r in rows]

    def create_lead(
        self,
        max_user_id: int,
        *,
        phone: str,
        ages: str,
        direction: str,
        full_name: str = "",
        source: str = "max",
    ) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO leads
                       (max_user_id, full_name, phone, ages, direction, source)
                   VALUES (?,?,?,?,?,?)""",
                (max_user_id, full_name, phone, ages, direction, source),
            )
            return cursor.lastrowid

    def get_leads(self, status: Optional[str] = "new") -> List[Dict[str, Any]]:
        query = "SELECT * FROM leads"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def get_lead(self, lead_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            return dict(row) if row else None

    def resolve_lead(self, lead_id: int, status: str, resolved_by: int) -> bool:
        """False — заявку уже закрыл другой менеджер."""
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE leads
                   SET status=?, resolved_at=CURRENT_TIMESTAMP, resolved_by=?
                   WHERE id=? AND status='new'""",
                (status, resolved_by, lead_id),
            )
            return cursor.rowcount > 0

    def count_leads(self) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM leads GROUP BY status"
            ).fetchall()
            return {r["status"]: r["cnt"] for r in rows}

    def get_lead_recipients(self) -> List[Dict[str, Any]]:
        """Уникальные адресаты рассылки среди оставивших заявку."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT max_user_id, MAX(full_name) AS full_name, MAX(phone) AS phone
                   FROM leads
                   WHERE max_user_id NOT IN (SELECT max_user_id FROM users WHERE is_active = 1)
                   GROUP BY max_user_id"""
            ).fetchall()
            return [dict(r) for r in rows]

    # ==================== ЗАЯВКИ НА ПЕРЕНОС ====================

    def create_transfer_request(
        self,
        max_user_id: int,
        lesson_id: Optional[Any],
        comment: str,
        author_role: str = "teacher",
    ) -> int:
        """
        lesson_id хранится СТРОКОЙ: id занятия может быть синтетическим
        ("116:2026-09-01"). Прежний int(lesson_id or 0) на таком id
        падал с ValueError и заявка не создавалась вовсе.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO transfer_requests
                       (teacher_max_id, lesson_id, comment, author_role)
                   VALUES (?,?,?,?)""",
                (max_user_id, str(lesson_id or ""), comment, author_role),
            )
            return cursor.lastrowid

    def get_transfer_request(self, request_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM transfer_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    def resolve_transfer_request(self, request_id: int, status: str, resolved_by: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE transfer_requests
                   SET status=?, resolved_at=CURRENT_TIMESTAMP, resolved_by=?
                   WHERE id=? AND status='pending'""",
                (status, resolved_by, request_id),
            )
            # False — заявку уже обработал другой менеджер.
            return cursor.rowcount > 0

    def get_pending_transfer_requests(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT tr.*,
                          u.full_name AS author_name,
                          u.phone     AS author_phone
                   FROM transfer_requests tr
                   LEFT JOIN users u ON tr.teacher_max_id = u.max_user_id
                   WHERE tr.status = 'pending'
                   ORDER BY tr.created_at DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    # ==================== НЕЯВКИ ====================

    def mark_absent(
        self,
        lesson_id: Any,
        client_id: int,
        client_name: str,
        lesson_date: str,
        teacher_max_id: Optional[int] = None,
    ) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO absences
                       (lesson_id, client_id, client_name, lesson_date, teacher_max_id)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(lesson_id, client_id) DO UPDATE SET
                       client_name=excluded.client_name,
                       lesson_date=excluded.lesson_date,
                       teacher_max_id=excluded.teacher_max_id,
                       status='pending',
                       resolved_at=NULL,
                       resolved_by=NULL""",
                (str(lesson_id), int(client_id), client_name, lesson_date, teacher_max_id),
            )
            return cursor.lastrowid

    def unmark_absent(self, lesson_id: Any, client_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM absences WHERE lesson_id=? AND client_id=?",
                (str(lesson_id), int(client_id)),
            )

    def get_absent_client_ids(self, lesson_id: Any) -> List[int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT client_id FROM absences WHERE lesson_id=?", (str(lesson_id),)
            ).fetchall()
            return [row["client_id"] for row in rows]

    def get_absence(self, absence_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM absences WHERE id=?", (absence_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_absences_for_date(
        self, lesson_date: str, status: Optional[str] = "pending"
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM absences WHERE lesson_date=?"
        params: List[Any] = [lesson_date]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at"
        with self._conn() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def resolve_absence(self, absence_id: int, status: str, resolved_by: int) -> bool:
        """False — неявку уже обработал другой менеджер."""
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE absences
                   SET status=?, resolved_at=CURRENT_TIMESTAMP, resolved_by=?
                   WHERE id=? AND status='pending'""",
                (status, resolved_by, absence_id),
            )
            return cursor.rowcount > 0

    # ==================== ПОДДЕРЖКА ====================

    def get_open_ticket_for_user(self, user_max_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM support_tickets WHERE user_max_id=? AND status='open' "
                "ORDER BY created_at DESC LIMIT 1",
                (user_max_id,),
            ).fetchone()
            return dict(row) if row else None

    def create_ticket(
        self, user_max_id: int, user_name: str, user_role: str, user_phone: str
    ) -> int:
        existing = self.get_open_ticket_for_user(user_max_id)
        if existing:
            return existing["id"]
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO support_tickets (user_max_id, user_name, user_role, user_phone)
                   VALUES (?,?,?,?)""",
                (user_max_id, user_name, user_role, user_phone),
            )
            return cursor.lastrowid

    def get_ticket(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM support_tickets WHERE id=?", (ticket_id,)
            ).fetchone()
            return dict(row) if row else None

    def add_ticket_message(
        self, ticket_id: int, sender_max_id: int, sender_side: str, text: str
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO support_messages (ticket_id, sender_max_id, sender_side, text)
                   VALUES (?,?,?,?)""",
                (ticket_id, sender_max_id, sender_side, text),
            )

    def get_ticket_messages(self, ticket_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM support_messages WHERE ticket_id=? "
                "ORDER BY created_at LIMIT ?",
                (ticket_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_open_tickets(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM support_tickets WHERE status='open' ORDER BY created_at"
            ).fetchall()
            return [dict(row) for row in rows]

    def close_ticket(self, ticket_id: int, closed_by: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE support_tickets
                   SET status='closed', closed_at=CURRENT_TIMESTAMP, closed_by=?
                   WHERE id=? AND status='open'""",
                (closed_by, ticket_id),
            )
            return cursor.rowcount > 0

    # ==================== СЧЁТЧИК БЕСПРИЧИННЫХ ЗАМОРОЗОК ====================
    #
    # Запасное хранилище на случай, когда в AlfaCRM не заведено
    # дополнительное поле под счётчик. Логика выбора хранилища — не
    # здесь, а в freezes.FreezeCounter: база не должна знать про CRM.

    def get_free_freezes(self, client_id: Any, default: int) -> int:
        """
        Остаток заморозок. Клиента, которого в таблице ещё нет, считаем
        не израсходовавшим ни одной: запись создаётся при первом
        списании, а не при первом просмотре.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT free_left FROM client_freezes WHERE client_id = ?",
                (int(client_id),),
            ).fetchone()
        return int(row["free_left"]) if row else max(0, int(default))

    def set_free_freezes(self, client_id: Any, value: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO client_freezes (client_id, free_left, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(client_id) DO UPDATE SET
                    free_left = excluded.free_left,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (int(client_id), max(0, int(value))),
            )

    # ==================== ЗАМОРОЗКИ ====================

    def add_freeze(
        self,
        client_id: int,
        *,
        kind: str,
        client_name: str = "",
        lesson_id: Any = None,
        lesson_date: str = "",
        reason: str = "",
        created_by: Optional[int] = None,
        created_by_role: str = "",
    ) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO freezes
                       (client_id, client_name, lesson_id, lesson_date, kind,
                        reason, created_by, created_by_role)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    int(client_id), client_name, str(lesson_id or ""), lesson_date,
                    kind, reason, created_by, created_by_role,
                ),
            )
            return cursor.lastrowid

    def get_freeze(self, freeze_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM freezes WHERE id=?", (freeze_id,)).fetchone()
            return dict(row) if row else None

    def get_freezes_by_client(self, client_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM freezes WHERE client_id=? ORDER BY created_at DESC LIMIT ?",
                (int(client_id), limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_freeze_for_lesson(
        self, client_id: int, lesson_id: Any
    ) -> Optional[Dict[str, Any]]:
        """
        Заморозка конкретного занятия у конкретного ученика.

        Нужна перед тем, как объявить занятие сгоревшим: если родитель
        успел заморозить, уведомление о сгорании было бы враньём.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM freezes WHERE client_id=? AND lesson_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (int(client_id), str(lesson_id or "")),
            ).fetchone()
            return dict(row) if row else None

    def get_freezes_awaiting_certificate(self) -> List[Dict[str, Any]]:
        """Заморозки по уважительной причине, для которых справка ещё не приложена."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM freezes WHERE kind='valid' AND certificate_file_id IS NULL "
                "ORDER BY created_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_freezes_with_certificate(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM freezes WHERE certificate_file_id IS NOT NULL "
                "ORDER BY certificate_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def attach_certificate(
        self, freeze_id: int, file_id: str, file_type: str = "photo"
    ) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE freezes
                   SET certificate_file_id=?, certificate_type=?,
                       certificate_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (file_id, file_type, freeze_id),
            )
            return cursor.rowcount > 0

    # ==================== АКТИВНОСТЬ В БОТЕ ====================

    def log_activity(self, max_user_id: int, user_name: str, event: str) -> None:
        """Записать одно действие посетителя."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO activity_log (max_user_id, user_name, event) VALUES (?,?,?)",
                (int(max_user_id), user_name or "", event or ""),
            )

    def get_recent_activity(
        self, users_limit: int = 15, events_per_user: int = 12
    ) -> List[Dict[str, Any]]:
        """
        Последние посетители, у каждого — его последние действия.

        Группировка по людям, а не сплошной лентой: менеджеру нужно
        понять, КТО приходил и что делал, а перемешанные события десяти
        человек этого не показывают. Внутри человека события идут в
        хронологическом порядке (как он их совершал), сами люди — по
        свежести последнего действия.
        """
        with self._conn() as conn:
            # Сортировка по MAX(id), а не по MAX(created_at): CURRENT_TIMESTAMP
            # в SQLite имеет секундную точность, и у нескольких человек,
            # зашедших в одну секунду, порядок был бы случайным.
            users = conn.execute(
                """SELECT max_user_id, MAX(created_at) AS last_at,
                          MAX(id) AS last_id, COUNT(*) AS total
                   FROM activity_log
                   GROUP BY max_user_id
                   ORDER BY last_id DESC
                   LIMIT ?""",
                (int(users_limit),),
            ).fetchall()

            result: List[Dict[str, Any]] = []
            for u in users:
                rows = conn.execute(
                    """SELECT event, user_name, created_at FROM activity_log
                       WHERE max_user_id=?
                       ORDER BY id DESC
                       LIMIT ?""",
                    (u["max_user_id"], int(events_per_user)),
                ).fetchall()
                events = [dict(r) for r in rows][::-1]  # обратно в хронологию
                result.append({
                    "max_user_id": u["max_user_id"],
                    "last_at": u["last_at"],
                    "total": u["total"],
                    "user_name": next(
                        (e["user_name"] for e in reversed(events) if e["user_name"]), ""
                    ),
                    "events": events,
                })
            return result

    def count_activity(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM activity_log").fetchone()
            return int(row["n"]) if row else 0

    def cleanup_activity(self, days: int = 14) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM activity_log WHERE created_at < datetime('now', ?)",
                (f"-{int(days)} days",),
            )
            return cursor.rowcount

    # ==================== ЛОГ НАПОМИНАНИЙ ====================

    def mark_reminder_sent(
        self,
        lesson_id: Any,
        reminder_type: str,
        target_max_id: Optional[int] = None,
    ):
        """
        Тип напоминания должен совпадать в записи и в проверке
        was_reminder_sent — иначе дедупликация никогда не сработает и
        напоминания задублируются на каждом тике планировщика.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO reminder_log (lesson_id, reminder_type, target_max_id) VALUES (?,?,?)",
                (str(lesson_id), reminder_type, target_max_id),
            )

    def was_reminder_sent(
        self,
        lesson_id: Any,
        reminder_type: str,
        target_max_id: Optional[int] = None,
        hours: int = 24,
    ) -> bool:
        query = """SELECT COUNT(*) AS count FROM reminder_log
                   WHERE lesson_id=? AND reminder_type=?
                     AND sent_at > datetime('now', ? || ' hours')"""
        params: List[Any] = [str(lesson_id), reminder_type, f"-{hours}"]
        if target_max_id is not None:
            query += " AND target_max_id=?"
            params.append(target_max_id)

        with self._conn() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
            return bool(row["count"])

    def cleanup_reminder_log(self, days: int = 30) -> int:
        """Лог напоминаний растёт вечно — чистим старое."""
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM reminder_log WHERE sent_at < datetime('now', ? || ' days')",
                (f"-{days}",),
            )
            return cursor.rowcount
