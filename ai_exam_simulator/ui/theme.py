"""Light and dark Qt style sheets."""

from __future__ import annotations

PALETTES = {
    "light": {
        "bg": "#f5f6f8", "surface": "#ffffff", "surface2": "#eef0f4", "border": "#d5d9e0", "text": "#1d2330",
        "muted": "#5b6474", "accent": "#2f6fdf", "accent_text": "#ffffff", "accent_soft": "#e3ecfc",
        "ok": "#1e8a4c", "warn": "#b26a00", "bad": "#c0392b", "side": "#1f2a3d", "side_text": "#d9e0ec",
        "side_sel": "#2f6fdf", "flag": "#d68a00",
    },
    "dark": {
        "bg": "#16191f", "surface": "#1f232b", "surface2": "#2a2f39", "border": "#3a404c", "text": "#e6e9ef",
        "muted": "#9aa3b2", "accent": "#5b8ff0", "accent_text": "#0d1117", "accent_soft": "#25324a",
        "ok": "#4cc38a", "warn": "#e0a040", "bad": "#ef6b5b", "side": "#0f1217", "side_text": "#c8d0dc",
        "side_sel": "#5b8ff0", "flag": "#e0a040",
    },
}


def colors(theme: str) -> dict[str, str]:
    return PALETTES.get(theme, PALETTES["light"])


def stylesheet(theme: str) -> str:
    c = colors(theme)
    return f"""
    QWidget {{ background: {c['bg']}; color: {c['text']}; font-family: "Segoe UI", Arial; font-size: 10pt; }}
    QMainWindow, QDialog {{ background: {c['bg']}; }}
    QLabel {{ background: transparent; }}
    QLabel[role="h1"] {{ font-size: 18pt; font-weight: 600; }}
    QLabel[role="h2"] {{ font-size: 13pt; font-weight: 600; }}
    QLabel[role="muted"] {{ color: {c['muted']}; }}
    QLabel[role="ok"] {{ color: {c['ok']}; font-weight: 600; }}
    QLabel[role="warn"] {{ color: {c['warn']}; font-weight: 600; }}
    QLabel[role="bad"] {{ color: {c['bad']}; font-weight: 600; }}
    QLabel[role="banner"] {{ background: {c['accent_soft']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 8px; }}
    QLabel[role="warnbanner"] {{ background: {c['surface2']}; border-left: 4px solid {c['warn']}; border-radius: 4px; padding: 8px; }}
    QLabel[role="timer"] {{ font-size: 20pt; font-weight: 700; font-family: Consolas, "Segoe UI"; }}
    QLabel[role="question"] {{ font-size: 12.5pt; }}
    QFrame[role="card"] {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 8px; }}
    QFrame[role="card"] QLabel {{ background: transparent; }}
    QListWidget#sidebar {{ background: {c['side']}; color: {c['side_text']}; border: none; font-size: 10.5pt; padding-top: 8px; }}
    QListWidget#sidebar::item {{ padding: 10px 16px; border-radius: 6px; margin: 2px 8px; }}
    QListWidget#sidebar::item:selected {{ background: {c['side_sel']}; color: white; }}
    QListWidget#sidebar::item:hover:!selected {{ background: rgba(255,255,255,0.07); }}
    QPushButton {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 6px 14px; }}
    QPushButton:hover {{ border-color: {c['accent']}; }}
    QPushButton:disabled {{ color: {c['muted']}; background: {c['surface2']}; }}
    QPushButton[role="primary"] {{ background: {c['accent']}; color: {c['accent_text']}; border: none; font-weight: 600; }}
    QPushButton[role="primary"]:disabled {{ background: {c['surface2']}; color: {c['muted']}; }}
    QPushButton[role="danger"] {{ color: {c['bad']}; }}
    QPushButton[role="nav"] {{ min-width: 34px; max-width: 34px; min-height: 30px; padding: 0; font-weight: 600; }}
    QPushButton[state="answered"] {{ background: {c['accent_soft']}; border-color: {c['accent']}; }}
    QPushButton[state="flagged"] {{ border: 2px solid {c['flag']}; }}
    QPushButton[state="current"] {{ background: {c['accent']}; color: {c['accent_text']}; }}
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit {{
        background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 5px; padding: 4px 6px;
        selection-background-color: {c['accent']}; selection-color: {c['accent_text']}; }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {c['accent']}; }}
    QTableWidget, QTableView, QListWidget, QTreeWidget {{ background: {c['surface']}; border: 1px solid {c['border']};
        border-radius: 6px; gridline-color: {c['border']}; alternate-background-color: {c['surface2']}; }}
    QHeaderView::section {{ background: {c['surface2']}; border: none; border-bottom: 1px solid {c['border']}; padding: 5px; font-weight: 600; }}
    QGroupBox {{ border: 1px solid {c['border']}; border-radius: 8px; margin-top: 14px; padding: 10px; background: {c['surface']}; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; font-weight: 600; }}
    QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QRadioButton {{ background: transparent; }}
    QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 6px; background: {c['surface']}; }}
    QTabBar::tab {{ padding: 7px 14px; background: {c['surface2']}; border: 1px solid {c['border']}; border-bottom: none;
        border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }}
    QTabBar::tab:selected {{ background: {c['surface']}; font-weight: 600; }}
    QProgressBar {{ border: 1px solid {c['border']}; border-radius: 5px; text-align: center; background: {c['surface2']}; height: 16px; }}
    QProgressBar::chunk {{ background: {c['accent']}; border-radius: 4px; }}
    QScrollArea {{ border: none; }}
    QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
    QToolTip {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']}; }}
    QStatusBar {{ background: {c['surface']}; border-top: 1px solid {c['border']}; }}
    """
