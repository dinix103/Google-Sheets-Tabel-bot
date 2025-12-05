# aiogram v3.x — Google Sheets attendance bot
# v10 (optimized + commented)
#
# Что сделано в этой версии:
#  • Исправлен ПЕРЕХОД ГОДА на длинных таблицах (декабрь → январь многократно).
#    Логика: один проход слева-направо по всем "дневным" колонкам с гарантией монотонности дат.
#  • Добавлены ПОДРОБНЫЕ КОММЕНТАРИИ по ключевым участкам.
#  • Ускорен расчёт дней по неделям: при загрузке мы заранее считаем суммы по каждой неделе
#    (по всем строкам сразу) и храним их в self.week_sums — запросы /days и кнопок работают быстрее.
#  • Исправлены мелкие ошибки и неточности:
#      - второй обработчик назывался cmd_start (на /help) — переименован в cmd_help;
#      - в /days при указании номера недели не заполнялся prefix → теперь ок;
#      - опечатки «Отработанно» → «Отработано»;
#      - форматные строки подправлены для ровного вывода.
#
# Зависимости:
#   pip install aiogram pandas gspread google-auth openpyxl
#
# Переменные окружения (или впишите прямо в код ниже):
#   BOT_TOKEN   — токен Telegram-бота
#   GSHEET_KEY  — ID Google Sheet (между /d/ и /edit в URL)
#   GWSHEET_NAME (optional) — имя вкладки (если не задано — берётся первая)
#
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional
from datetime import date

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command

import pandas as pd
import numpy as np
import gspread  # pip install gspread google-auth


# ------------------- Константы и справочники -------------------
WEEKDAYS_RU = ["сб", "вс", "пн", "вт", "ср", "чт", "пт"]
MONTHS_RU = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12
}
MONTH_NAMES = {v: k.capitalize() for k, v in MONTHS_RU.items()}

# ставка за день
DAY_RATE = 3000


# ------------------- Конфиги -------------------
@dataclass
class BotConfig:
    default_year: int = date.today().year


@dataclass
class GSConfig:
    # По желанию можно вписать дефолты прямо здесь:
    sheet_key: str = os.environ.get("GSHEET_KEY", "#YOUR_GHEET_KEY")
    worksheet_name: Optional[str] = "табель" 
    # Сервисный ключ рядом с .py файлом
    creds_path: str = os.path.join(os.path.dirname(__file__), "service_account.json")


# ------------------- Доступ к Google Sheets -------------------
class GSReader:
    def __init__(self, cfg: GSConfig):
        self.cfg = cfg
        self._gc = None
        self._ws = None

    def connect(self):
        """Открываем таблицу и лист. Ищем service_account.json локально рядом с файлом."""
        if not self.cfg.sheet_key:
            raise RuntimeError("GSHEET_KEY не задан. Вставь ID таблицы (между /d/ и /edit в URL).")
        if not os.path.exists(self.cfg.creds_path):
            raise FileNotFoundError(
                f"Не найден service_account.json: {self.cfg.creds_path}\n"
                "Положи его рядом с .py файлом."
            )
        self._gc = gspread.service_account(filename=self.cfg.creds_path)
        sh = self._gc.open_by_key(self.cfg.sheet_key)
        self._ws = sh.worksheet(self.cfg.worksheet_name) if self.cfg.worksheet_name else sh.get_worksheet(0)

    def to_dataframe(self) -> pd.DataFrame:
        """Читаем все ячейки листа в pandas.DataFrame. Сохраняем 'как раньше' преобразование."""
        if self._ws is None:
            self.connect()
        values = self._ws.get_all_values()
        if not values:
            raise RuntimeError("Пустой лист Google Sheets.")
        df = pd.DataFrame(values)
        # Оставляем старое поведение (возможны FutureWarning — приемлемо):
        df = df.replace({"": np.nan})
        df = df.apply(pd.to_numeric, errors="ignore")
        return df


# ------------------- Модель таблицы посещаемости -------------------
class AttendanceTable:
    """Инкапсулирует логику разбора 'шапки', недель (сб–пт), дат и сумм по неделям."""

    def __init__(self, bcfg: BotConfig, gscfg: GSConfig):
        self.bcfg = bcfg
        self.gscfg = gscfg
        self.df: Optional[pd.DataFrame] = None

        # Списки колонок по неделям: каждая неделя = список из 7 индексов столбцов
        self.week_columns: List[List[int]] = []

        # Диапазоны дат по неделям (start_date, end_date)
        self.week_ranges: List[Tuple[Optional[date], Optional[date]]] = []

        # Места колонок с идентификаторами/именами (по умолчанию как было)
        self.id_col = None
        self.name_col = 2
        self.role_col = 3

        # Номер строки, где стоят дни недели (сб–пт)
        self.header_weekdays_row: Optional[int] = None

        # Дата каждой "дневной" колонки после предрасчёта
        self.col_dates: Dict[int, Optional[date]] = {}

        # Предрасчитанные суммы по неделям для всех строк
        # week_sums[week_no] -> pandas.Series (index=row, value=sum)
        self.week_sums: Dict[int, pd.Series] = {}

        self.reader = GSReader(gscfg)

    # ---------- Внутренние вспомогательные ----------
    def _is_weekday_cell(self, v: object) -> bool:
        """Ячейка является коротким названием дня недели (ru)."""
        return isinstance(v, str) and v.strip().lower() in WEEKDAYS_RU

    def _detect_id_col(self):
        """Автоопределение колонки Telegram ID: по шапке или по «похожести» на числовую ID-колонку."""
        tg_keywords = {"telegram id", "tg id", "tg_id", "телеграм id", "телеграм", "айди", "id", "id телеграм"}
        self.id_col = None

        # 1) ищем в первых строках по ключевым словам
        for i in range(min(5, len(self.df))):
            for j, val in enumerate(self.df.iloc[i]):
                if isinstance(val, str):
                    low = val.strip().lower()
                    if (low in tg_keywords) or ("telegram" in low) or ("телеграм" in low):
                        self.id_col = j
                        return

        # 2) эвристика: выбираем «самую числовую» из первых нескольких колонок (если не нашли явно)
        counts = []
        for j in range(min(6, self.df.shape[1])):
            col = self.df.iloc[2:, j]
            numeric_like = sum(
                1 for v in col
                if isinstance(v, (int, float, np.integer, np.floating)) or (isinstance(v, str) and v.strip().isdigit())
            )
            counts.append((numeric_like, j))
        counts.sort(reverse=True)
        if counts and counts[0][0] >= 3:
            self.id_col = counts[0][1]

    def _find_header_row(self) -> Tuple[int, List[int]]:
        """Находим строку, в которой подряд встречаются 7 заголовков дней недели (сб–пт)."""
        header_row_idx = None
        weekday_cols: List[int] = []
        for i in range(len(self.df)):
            row = self.df.iloc[i]
            cols = [j for j, v in enumerate(row) if self._is_weekday_cell(v)]
            if len(cols) >= 7:
                header_row_idx = i
                weekday_cols = cols
                break
        if header_row_idx is None:
            raise RuntimeError("Не найдена строка с днями недели (сб–пт).")
        return header_row_idx, weekday_cols

    def _build_month_ctx(self, header_row_idx: int) -> Dict[int, int]:
        """Для каждой дневной колонки пытаемся понять месяц из шапки (строка выше).
        Результат: словарь {col_index: month_int}"""
        ctx: Dict[int, int] = {}
        r = header_row_idx - 1
        if r < 0:
            return ctx
        current = None
        for c in range(self.df.shape[1]):
            v = self.df.iat[r, c]
            if isinstance(v, str):
                low = v.strip().lower()
                if low in MONTHS_RU:
                    current = MONTHS_RU[low]
            if current is not None:
                ctx[c] = current
        return ctx

    def _daynum_at(self, header_row_idx: int, c: int) -> Optional[int]:
        """Возвращает номер дня (1..31) для колонки c.
        Ищем в строке над заголовком (или на соседней справа), как в предыдущих версиях."""
        r = header_row_idx - 1
        if r < 0:
            return None
        for cc in (c, c + 1):
            if 0 <= cc < self.df.shape[1]:
                v = self.df.iat[r, cc]
                try:
                    if isinstance(v, (int, float, np.integer, np.floating)) and not pd.isna(v):
                        return int(v)
                    if isinstance(v, str) and v.strip().isdigit():
                        return int(v.strip())
                except Exception:
                    pass
        return None

    def _compose_weeks(self, weekday_cols: List[int]) -> None:
        """Группируем все найденные «дневные» колоноки по 7 штук (сб–пт) → неделя."""
        self.week_columns = []
        for k in range(0, len(weekday_cols), 7):
            chunk = weekday_cols[k:k + 7]
            if len(chunk) == 7:
                self.week_columns.append(chunk)

    def _precompute_column_dates(self, header_row_idx: int, month_ctx: Dict[int, int]) -> None:
        """Один проход слева-направо по всем "дневным" колонкам.
        Строим self.col_dates[c] = точную дату, соблюдая монотонность по времени.
        Если дата «откатывается назад», увеличиваем год.
        """
        from datetime import date as _d
        self.col_dates = {}
        all_cols = [c for chunk in self.week_columns for c in chunk]
        if not all_cols:
            return

        base_year = self.bcfg.default_year
        current_year = base_year
        prev_month: Optional[int] = None
        prev_daynum: Optional[int] = None
        last_date: Optional[_d] = None

        # стартовый месяц — по первой колонке
        if all_cols:
            prev_month = month_ctx.get(all_cols[0], None)

        for c in all_cols:
            daynum = self._daynum_at(header_row_idx, c)
            explicit_month = month_ctx.get(c, None)

            # 1) База: месяц из шапки важнее
            if explicit_month is not None:
                if prev_month is not None and explicit_month < prev_month:
                    current_year += 1  # например, декабрь → январь
                month_here = explicit_month
            else:
                # 2) Если в шапке нет явного месяца — пытаемся угадать по падению дня (31 → 1)
                if prev_month is None:
                    month_here = month_ctx.get(c, None) or month_ctx.get(all_cols[0], None)
                else:
                    if prev_daynum is not None and daynum is not None and daynum < prev_daynum:
                        month_here = 1 if prev_month == 12 else prev_month + 1
                        if month_here == 1:
                            current_year += 1
                    else:
                        month_here = prev_month

            prev_month = month_here if month_here is not None else prev_month
            prev_daynum = daynum if daynum is not None else prev_daynum

            # 3) Формируем дату и обеспечиваем монотонность
            if daynum is not None and month_here is not None:
                try:
                    d = _d(current_year, month_here, daynum)
                except Exception:
                    d = None

                # Если дата меньше предыдущей — поднимаем год и пробуем ещё раз
                if d is not None and last_date is not None and d < last_date:
                    try:
                        d = _d(current_year + 1, month_here, daynum)
                        current_year += 1
                    except Exception:
                        pass

                self.col_dates[c] = d
                if d is not None:
                    last_date = d
            else:
                self.col_dates[c] = None

    def _compose_week_ranges(self) -> None:
        """Строим диапазоны по self.col_dates: min..max для каждой недели."""
        self.week_ranges = []
        for cols in self.week_columns:
            dates = [self.col_dates.get(c) for c in cols]
            real = [d for d in dates if d is not None]
            self.week_ranges.append((min(real) if real else None, max(real) if real else None))

    def _precompute_week_sums(self) -> None:
        """Оптимизация: один раз считаем суммы по каждой неделе для всех сотрудников.
        Это быстрее, чем суммировать в каждом запросе по ячейкам.
        """
        self.week_sums = {}
        if self.df is None or self.header_weekdays_row is None:
            return
        first_row = self.header_weekdays_row + 1  # данные начинаются со следующей строки
        # Для каждой недели берём срез столбцов и считаем сумму по строкам
        for idx, cols in enumerate(self.week_columns, start=1):
            # Преобразуем содержимое недели в числовое и суммируем
            block = self.df.iloc[first_row:, cols]
            block_num = block.apply(pd.to_numeric, errors="coerce").fillna(0)
            sums = block_num.sum(axis=1)  # Series с индексами исходных строк
            # Храним Series так, чтобы можно было обратиться по row_idx
            self.week_sums[idx] = sums

    def load(self) -> str:
        """Главная точка — прочитать лист и подготовить все структуры."""
        self.df = self.reader.to_dataframe()
        self._detect_id_col()
        header_row_idx, weekday_cols = self._find_header_row()
        self.header_weekdays_row = header_row_idx
        self._compose_weeks(weekday_cols)
        month_ctx = self._build_month_ctx(header_row_idx)

        # 1) Предрасчёт дат колонок с гарантией монотонности
        self._precompute_column_dates(header_row_idx, month_ctx)
        # 2) Диапазоны недель
        self._compose_week_ranges()
        # 3) Суммы по неделям
        self._precompute_week_sums()

        return "Прочитал Google Sheet."

    # --- Утилиты для команд/кнопок ---
    def weeks_of_current_month(self) -> List[int]:
        """Возвращает список глобальных индексов недель, пересекающих текущий месяц/год."""
        today = date.today()
        m, y = today.month, today.year
        indices = []
        for idx, (s, e) in enumerate(self.week_ranges, start=1):
            if not s or not e:
                continue
            if (s.year == y and s.month == m) or (e.year == y and e.month == m):
                indices.append(idx)
        return indices

    def current_week_of_current_month(self) -> Optional[Tuple[int, int]]:
        """Возвращает (локальный_номер_в_месяце, глобальный_индекс_недели) для текущей даты.
        Если сегодня не попадает ни в один диапазон — берём последнюю завершённую внутри месяца.
        """
        today = date.today()
        month_weeks = self.weeks_of_current_month()
        if not month_weeks:
            return None

        # Сегодня внутри одной из недель
        for local_no, gidx in enumerate(month_weeks, start=1):
            s, e = self.week_ranges[gidx - 1]
            if s and e and s <= today <= e:
                return (local_no, gidx)

        # Иначе — последняя завершённая
        finished = [
            (ln, g) for ln, g in enumerate(month_weeks, start=1)
            if self.week_ranges[g - 1][1] and self.week_ranges[g - 1][1] <= today
        ]
        if finished:
            return finished[-1]

        # Запасной: последняя неделя месяца
        return (len(month_weeks), month_weeks[-1])

    def week_days(self, row_idx: int, week_no: int) -> int:
        """Быстрый доступ к сумме по предрасчитанным значениям."""
        if week_no < 1 or week_no > len(self.week_columns):
            raise ValueError(f"Неделя №{week_no} вне диапазона (1..{len(self.week_columns)}).")
        series = self.week_sums.get(week_no)
        if series is None:
            return 0
        # series индексирован исходными строками (начиная с header_weekdays_row+1)
        val = series.get(row_idx, 0)
        try:
            return int(val)
        except Exception:
            return int(float(val) if pd.notna(val) else 0)

    def week_range_str(self, week_no: int) -> str:
        if 1 <= week_no <= len(self.week_ranges):
            start, end = self.week_ranges[week_no - 1]
            if start and end:
                return f"{start.strftime('%d.%m.%Y')}–{end.strftime('%d.%m.%Y')}"
        return ""

    def _row_for_id(self, tg_id: int) -> Optional[int]:
        """Находим строку сотрудника по Telegram ID."""
        if self.id_col is None or self.df is None or self.header_weekdays_row is None:
            return None
        for i in range(self.header_weekdays_row + 1, len(self.df)):
            v = self.df.iat[i, self.id_col]
            if pd.isna(v):
                continue
            try:
                if int(str(v).strip()) == int(tg_id):
                    return i
            except Exception:
                continue
        return None


# ------------------- Бот и хендлеры -------------------
router = Router()
GSCFG = GSConfig()
BCFG = BotConfig()
ATT = AttendanceTable(BCFG, GSCFG)

# Per-user: выбранная (локальная) неделя внутри текущего месяца
USER_STATE: Dict[int, int] = {}

def build_menu() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура (ReplyKeyboard), чтобы кнопки не 'плавали'."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗓 Показать текущую неделю")],
            [KeyboardButton(text="📅 Отработанные дни"), KeyboardButton(text="💰 Моя зарплата")],
            [KeyboardButton(text="🔄 Перечитать таблицу"), KeyboardButton(text="Мой ID")],
            [KeyboardButton(text="👤 Моя привязка")],
        ],
        resize_keyboard=True
    )


@router.message(Command("start"))
async def cmd_start(msg: Message):
    """Старт: определяем текущую неделю и показываем меню."""
    cur = ATT.current_week_of_current_month()
    if cur:
        USER_STATE[msg.from_user.id] = cur[0]
    today = date.today()
    await msg.answer(
        f"👋 Привет! Сегодня: {today.strftime('%d.%m.%Y')}.\n\n"
        "Пользуйся меню с кнопками ниже.",
        reply_markup=build_menu()
    )


@router.message(Command("help"))
async def cmd_help(msg: Message):
    """Справка по командам — отдельный обработчик (не перекрывает /start)."""
    cur = ATT.current_week_of_current_month()
    if cur:
        USER_STATE[msg.from_user.id] = cur[0]
    await msg.answer(
        "🆘 Команды бота:\n"
        "/days [номер недели] — дни за указанную или текущую неделю месяца\n"
        "/weeks — показать все недели (с датами)\n"
        "/reload — перечитать Google Sheet\n"
        "/recent — (удалено, см. кнопки)\n"
        "/salary — показать зарплату за указанную или текущую неделю месяца\n"
        "/me — показать твою привязку в таблице\n",
        reply_markup=build_menu()
    )


@router.message(F.text == "Мой ID")
async def my_id_button(msg: Message):
    await msg.answer(f"Твой Telegram ID: `{msg.from_user.id}`", parse_mode="Markdown")


@router.message(Command("reload"))
@router.message(F.text == "🔄 Перечитать таблицу")
async def cmd_reload(msg: Message):
    """Перечитать таблицу и пересчитать все индексы/суммы."""
    try:
        info = ATT.load()
        cur = ATT.current_week_of_current_month()
        if cur:
            USER_STATE[msg.from_user.id] = cur[0]
        await msg.answer("✅ " + info, reply_markup=build_menu())
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}", reply_markup=build_menu())


@router.message(Command("weeks"))
async def cmd_weeks(msg: Message):
    """Показать все недели (глобальная нумерация) + даты."""
    if ATT.week_columns:
        lines = []
        for i in range(1, len(ATT.week_columns) + 1):
            rng = ATT.week_range_str(i)
            lines.append(f"Глобальная неделя №{i}" + (f" ({rng})" if rng else ""))
        await msg.answer("Все недели:\n" + "\n".join("• " + x for x in lines), reply_markup=build_menu())
    else:
        await msg.answer("Сначала /reload", reply_markup=build_menu())


def parse_week_only(text: str) -> Optional[int]:
    """Парсим номер недели из команды: '/days 12' → 12."""
    parts = text.strip().split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None


@router.message(Command("days"))
async def cmd_days(msg: Message):
    """Показать количество отработанных дней за неделю (указанную или текущую)."""
    try:
        week = parse_week_only(msg.text or "")
        row = ATT._row_for_id(msg.from_user.id)
        if row is None:
            await msg.answer("Не нашёл твой ID в таблице. Попроси админа занести твой Telegram ID в столбец ID и сделай /reload.", reply_markup=build_menu())
            return

        if not week:
            cur = ATT.current_week_of_current_month()
            if not cur:
                await msg.answer("Не смог определить текущую неделю текущего месяца. Обратись к администратору.", reply_markup=build_menu())
                return
            local_no, week = cur
            prefix = "📅 Отработано за"
        else:
            prefix = f"Глобальная неделя №{week}"

        days = ATT.week_days(row, week)
        name = ATT.df.iat[row, ATT.name_col]
        rng = ATT.week_range_str(week)
        suffix = f" ({rng})" if rng else ""
        await msg.answer(f"{prefix}{suffix}: {name} — {days} дн.", reply_markup=build_menu())
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}", reply_markup=build_menu())


@router.message(Command("salary"))
async def cmd_salary(msg: Message):
    """Показать зарплату за неделю (указанную или текущую)."""
    try:
        week = parse_week_only(msg.text or "")
        row = ATT._row_for_id(msg.from_user.id)
        if row is None:
            await msg.answer("Не нашёл твой ID в таблице. Попроси админа занести твой Telegram ID в столбец ID и сделай /reload.", reply_markup=build_menu())
            return

        if not week:
            cur = ATT.current_week_of_current_month()
            if not cur:
                await msg.answer("Не смог определить текущую неделю текущего месяца. Проверь шапку таблицы и /reload.", reply_markup=build_menu())
                return
            local_no, week = cur
            prefix = "💰 Твоя зарплата за"
        else:
            prefix = f"Глобальная неделя №{week}"

        days = ATT.week_days(row, week)
        salary = days * DAY_RATE
        name = ATT.df.iat[row, ATT.name_col]
        rng = ATT.week_range_str(week)
        suffix = f" ({rng})" if rng else ""
        await msg.answer(f"{prefix}{suffix}:\n{name} — {days} дн × {DAY_RATE} = {salary} ₽", reply_markup=build_menu())
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}", reply_markup=build_menu())


# ----- Кнопки постоянного меню -----
@router.message(F.text == "🗓 Показать текущую неделю")
async def kb_current_week(msg: Message):
    cur = ATT.current_week_of_current_month()
    if not cur:
        await msg.answer("Не смог определить текущую неделю текущего месяца. Нажми «🔄 Перечитать таблицу» или обратись к администратору.", reply_markup=build_menu())
        return
    USER_STATE[msg.from_user.id] = cur[0]
    local_no, gidx = cur
    rng = ATT.week_range_str(gidx)
    await msg.answer("🗓 Текущая неделя:" + (f" {rng}" if rng else ""), reply_markup=build_menu())


@router.message(F.text == "📅 Отработанные дни")
async def kb_my_days_current(msg: Message):
    row = ATT._row_for_id(msg.from_user.id)
    if row is None:
        await msg.answer("Твой ID не найден в таблице. Нажми «🔄 Перечитать таблицу» или обратись к администратору.", reply_markup=build_menu())
        return

    month_weeks = ATT.weeks_of_current_month()
    if not month_weeks:
        await msg.answer("Ошибка: нет недель в текущем месяце. Обратись к администратору.", reply_markup=build_menu())
        return

    cur_local = USER_STATE.get(msg.from_user.id) or (ATT.current_week_of_current_month() or (1, month_weeks[0]))[0]
    gidx = month_weeks[cur_local - 1]

    days = ATT.week_days(row, gidx)
    name = ATT.df.iat[row, ATT.name_col]
    rng = ATT.week_range_str(gidx)
    suffix = f" ({rng})" if rng else ""
    await msg.answer(f"📅 Отработано за{suffix}: {name} — {days} дн.", reply_markup=build_menu())


@router.message(F.text == "💰 Моя зарплата")
async def kb_my_salary_current(msg: Message):
    row = ATT._row_for_id(msg.from_user.id)
    if row is None:
        await msg.answer("Твой ID не найден в таблице. Нажми «🔄 Перечитать таблицу» или обратись к администратору.", reply_markup=build_menu())
        return

    month_weeks = ATT.weeks_of_current_month()
    if not month_weeks:
        await msg.answer("Ошибка: нет недель в текущем месяце. Обратись к администратору.", reply_markup=build_menu())
        return

    cur_local = USER_STATE.get(msg.from_user.id) or (ATT.current_week_of_current_month() or (1, month_weeks[0]))[0]
    gidx = month_weeks[cur_local - 1]

    days = ATT.week_days(row, gidx)
    salary = days * DAY_RATE
    name = ATT.df.iat[row, ATT.name_col]
    rng = ATT.week_range_str(gidx)
    suffix = f" ({rng})" if rng else ""
    await msg.answer(f"💰 Твоя зарплата за{suffix}: {name} — {days} дн × {DAY_RATE} = {salary} ₽", reply_markup=build_menu())


@router.message(F.text == "👤 Моя привязка")
async def kb_me(msg: Message):
    await cmd_me(msg)


# ---------- /me ----------
@router.message(Command("me"))
async def cmd_me(msg: Message):
    try:
        row = ATT._row_for_id(msg.from_user.id)
        if row is None:
            await msg.answer("Привязка по ID не найдена. Убедись, что твой ID занесён в таблицу и сделай /reload.", reply_markup=build_menu())
            return
        name = ATT.df.iat[row, ATT.name_col]
        await msg.answer(f"Ты привязан к: {name} (строка {row})", reply_markup=build_menu())
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}", reply_markup=build_menu())


# ------------------- Старт бота -------------------
async def on_startup(bot: Bot):
    """Пробуем загрузить таблицу при старте (если не удастся — можно /reload)."""
    try:
        info = ATT.load()
        logging.info(info)
    except Exception as e:
        logging.warning("Не удалось прочитать Google Sheet при старте: %s", e)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    
    # ⚠️ Можно вписать токен прямо здесь (или использовать переменную окружения BOT_TOKEN):
    token = os.environ.get("BOT_TOKEN") or "#YOUR_TOKEN!!!!"
    if not token or token == "PASTE_YOUR_TOKEN_HERE":
        raise RuntimeError("Не задан BOT_TOKEN: впиши прямо в код или через переменную окружения.")
    

    dp = Dispatcher()
    dp.include_router(router)
    bot = Bot(token=token)

    await on_startup(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
