"""Page identifiers for the main window navigation."""

from __future__ import annotations

from enum import Enum


class Page(str, Enum):
    DASHBOARD = "dashboard"
    CREATE = "create"
    TAKE = "take"
    RESULTS = "results"
    BANK = "bank"
    MATERIALS = "materials"
    STATISTICS = "statistics"
    AI_SETTINGS = "ai_settings"
    APP_SETTINGS = "app_settings"


PAGE_TITLES = {
    Page.DASHBOARD: "🏠  Dashboard",
    Page.CREATE: "📝  Create Exam",
    Page.TAKE: "⏱  Take Exam",
    Page.RESULTS: "📊  Results",
    Page.BANK: "🗂  Question Bank",
    Page.MATERIALS: "📚  Study Materials",
    Page.STATISTICS: "📈  Statistics",
    Page.AI_SETTINGS: "🤖  AI Settings",
    Page.APP_SETTINGS: "⚙  Settings",
}
