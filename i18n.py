"""
JeanClaudeCombien — Translations
To add a new language: copy any block, give it a new code, translate the values.
Keys must stay exactly as-is; only values get translated.
"""

# Shown in the language picker menu
LANGUAGES = {
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "ru": "Русский",
    "lg": "Luganda",
}

STRINGS = {
    "en": {
        "row_5h":        "5h window",
        "row_week":      "Week",
        "row_sonnet":    "Sonnet",
        "row_design":    "Design",
        "row_credits":   "Credits",
        "reset_done":    "↺ reset",
        "tokens_today":  "🔢  {n} tokens today",
        "menu_compact":  "→ Compact mode",
        "menu_full":     "→ Full mode",
        "menu_refresh":  "↺  Refresh now",
        "menu_interval": "⏰  Interval",
        "menu_opacity":  "👁  Opacity",
        "menu_language": "🌐  Language",
        "menu_close":    "✕  Close",
        "int_1m":   "1 min",
        "int_5m":   "5 min",
        "int_10m":  "10 min",
        "int_30m":  "30 min",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    },

    "fr": {
        "row_5h":        "Fenêtre 5h",
        "row_week":      "Semaine",
        "row_sonnet":    "Sonnet",
        "row_design":    "Design",
        "row_credits":   "Crédits",
        "reset_done":    "↺ réinit.",
        "tokens_today":  "🔢  {n} tokens aujourd'hui",
        "menu_compact":  "→ Mode compact",
        "menu_full":     "→ Mode complet",
        "menu_refresh":  "↺  Actualiser",
        "menu_interval": "⏰  Intervalle",
        "menu_opacity":  "👁  Opacité",
        "menu_language": "🌐  Langue",
        "menu_close":    "✕  Fermer",
        "int_1m":   "1 min",
        "int_5m":   "5 min",
        "int_10m":  "10 min",
        "int_30m":  "30 min",
        "days": ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"],
    },

    "es": {
        "row_5h":        "Ventana 5h",
        "row_week":      "Semana",
        "row_sonnet":    "Sonnet",
        "row_design":    "Diseño",
        "row_credits":   "Créditos",
        "reset_done":    "↺ reinic.",
        "tokens_today":  "🔢  {n} tokens hoy",
        "menu_compact":  "→ Modo compacto",
        "menu_full":     "→ Modo completo",
        "menu_refresh":  "↺  Actualizar",
        "menu_interval": "⏰  Intervalo",
        "menu_opacity":  "👁  Opacidad",
        "menu_language": "🌐  Idioma",
        "menu_close":    "✕  Cerrar",
        "int_1m":   "1 min",
        "int_5m":   "5 min",
        "int_10m":  "10 min",
        "int_30m":  "30 min",
        "days": ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"],
    },

    "ru": {
        "row_5h":        "5ч окно",
        "row_week":      "Неделя",
        "row_sonnet":    "Sonnet",
        "row_design":    "Дизайн",
        "row_credits":   "Кредиты",
        "reset_done":    "↺ сброс",
        "tokens_today":  "🔢  {n} токенов сегодня",
        "menu_compact":  "→ Компактный",
        "menu_full":     "→ Полный режим",
        "menu_refresh":  "↺  Обновить",
        "menu_interval": "⏰  Интервал",
        "menu_opacity":  "👁  Прозрачность",
        "menu_language": "🌐  Язык",
        "menu_close":    "✕  Закрыть",
        "int_1m":   "1 мин",
        "int_5m":   "5 мин",
        "int_10m":  "10 мин",
        "int_30m":  "30 мин",
        "days": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    },

    "lg": {
        # Luganda — spoken in Uganda
        "row_5h":        "Saawa 5",       # hour 5
        "row_week":      "Sabbiiti",      # week / Sunday
        "row_sonnet":    "Sonnet",
        "row_design":    "Design",
        "row_credits":   "Ensimbi",       # money / credits
        "reset_done":    "↺ okuddamu",   # to do again
        "tokens_today":  "🔢  {n} ebikomo leero",  # limits today
        "menu_compact":  "→ Entono",      # small
        "menu_full":     "→ Enzijuvu",    # full
        "menu_refresh":  "↺  Ddamu",      # do again
        "menu_interval": "⏰  Ebbanga",    # interval / space
        "menu_opacity":  "👁  Okwolesebwa", # visibility
        "menu_language": "🌐  Olulimi",    # language
        "menu_close":    "✕  Galawo",     # goodbye / close
        "int_1m":   "1 eddakiika",        # minute
        "int_5m":   "5 eddakiika",
        "int_10m":  "10 eddakiika",
        "int_30m":  "30 eddakiika",
        "days": ["Bba", "Lbi", "Lsa", "Lna", "Lta", "Lmu", "Sab"],
        # Mon=Bbalaza, Tue=Lwakubiri, Wed=Lwakusatu,
        # Thu=Lwakuna, Fri=Lwakutaano, Sat=Lwamukaaga, Sun=Sabbiiti
    },
}


def get(lang: str, key: str, **kwargs):
    """
    Return translated value for lang/key.
    Falls back to English if key or lang is missing.
    Supports .format(**kwargs) for strings with placeholders.
    """
    val = (STRINGS.get(lang) or STRINGS["en"]).get(key)
    if val is None:
        val = STRINGS["en"].get(key, key)
    if isinstance(val, str) and kwargs:
        return val.format(**kwargs)
    return val
