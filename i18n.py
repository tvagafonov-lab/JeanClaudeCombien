"""
JeanClaudeCombien — Translations
To add a new language: copy any block, give it a new code, translate the values.
Keys must stay exactly as-is; only values get translated.
"""

LANGUAGES = {
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "ru": "Русский",
    "lg": "Luganda",
}

STRINGS = {
    "en": {
        "row_5h":              "5h window",
        "row_week":            "Week",
        "row_sonnet":          "Sonnet",
        "row_design":          "Design",
        "row_credits":         "Credits",
        "reset_done":          "↺ reset",
        "menu_compact":        "→ Compact mode",
        "menu_full":           "→ Full mode",
        "menu_show_used":      "% Show used",
        "menu_show_remaining": "% Show remaining",
        "menu_opacity":        "👁  Opacity",
        "menu_language":       "🌐  Language",
        "menu_dock":           "⊞  Dock mode",
        "menu_exit_dock":      "↑  Exit dock",
        "menu_close":          "✕  Close",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    },

    "fr": {
        "row_5h":              "Fenêtre 5h",
        "row_week":            "Semaine",
        "row_sonnet":          "Sonnet",
        "row_design":          "Design",
        "row_credits":         "Crédits",
        "reset_done":          "↺ réinit.",
        "menu_compact":        "→ Mode compact",
        "menu_full":           "→ Mode complet",
        "menu_show_used":      "% Afficher utilisé",
        "menu_show_remaining": "% Afficher restant",
        "menu_opacity":        "👁  Opacité",
        "menu_language":       "🌐  Langue",
        "menu_dock":           "⊞  Mode dock",
        "menu_exit_dock":      "↑  Quitter le dock",
        "menu_close":          "✕  Fermer",
        "days": ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"],
    },

    "es": {
        "row_5h":              "Ventana 5h",
        "row_week":            "Semana",
        "row_sonnet":          "Sonnet",
        "row_design":          "Diseño",
        "row_credits":         "Créditos",
        "reset_done":          "↺ reinic.",
        "menu_compact":        "→ Modo compacto",
        "menu_full":           "→ Modo completo",
        "menu_show_used":      "% Mostrar usado",
        "menu_show_remaining": "% Mostrar restante",
        "menu_opacity":        "👁  Opacidad",
        "menu_language":       "🌐  Idioma",
        "menu_dock":           "⊞  Modo dock",
        "menu_exit_dock":      "↑  Salir del dock",
        "menu_close":          "✕  Cerrar",
        "days": ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"],
    },

    "ru": {
        "row_5h":              "5ч окно",
        "row_week":            "Неделя",
        "row_sonnet":          "Sonnet",
        "row_design":          "Дизайн",
        "row_credits":         "Кредиты",
        "reset_done":          "↺ сброс",
        "menu_compact":        "→ Компактный",
        "menu_full":           "→ Полный режим",
        "menu_show_used":      "% Показать использование",
        "menu_show_remaining": "% Показать остаток",
        "menu_opacity":        "👁  Прозрачность",
        "menu_language":       "🌐  Язык",
        "menu_dock":           "⊞  Режим дока",
        "menu_exit_dock":      "↑  Выйти из дока",
        "menu_close":          "✕  Закрыть",
        "days": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    },

    "lg": {
        # Luganda — spoken in Uganda
        "row_5h":              "Saawa 5",
        "row_week":            "Sabbiiti",
        "row_sonnet":          "Sonnet",
        "row_design":          "Design",
        "row_credits":         "Ensimbi",
        "reset_done":          "↺ okuddamu",
        "menu_compact":        "→ Entono",
        "menu_full":           "→ Enzijuvu",
        "menu_show_used":      "% Okozikozika",
        "menu_show_remaining": "% Okusigalawo",
        "menu_opacity":        "👁  Okwolesebwa",
        "menu_language":       "🌐  Olulimi",
        "menu_dock":           "⊞  Dock",
        "menu_exit_dock":      "↑  Okuva Dock",
        "menu_close":          "✕  Galawo",
        "days": ["Bba", "Lbi", "Lsa", "Lna", "Lta", "Lmu", "Sab"],
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
