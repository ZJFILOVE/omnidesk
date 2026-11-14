"""Semantic colors shared by the native desktop views."""

from PyQt6.QtGui import QColor, QFont, QPalette


from ui_tokens import THEMES, UI_FONT_FAMILIES


def apply_theme(app, name):
    c = THEMES.get(name, THEMES["light"])
    font = QFont()
    font.setFamilies(UI_FONT_FAMILIES)
    font.setPixelSize(13)
    app.setFont(font)
    palette = QPalette()
    for role, token in (("Window", "base"), ("WindowText", "text"), ("Base", "surface"),
                        ("AlternateBase", "soft"), ("Text", "text"), ("Button", "surface"),
                        ("ButtonText", "text"), ("Highlight", "accent"),
                        ("HighlightedText", "on_accent"), ("ToolTipBase", "surface"),
                        ("ToolTipText", "text"), ("PlaceholderText", "muted")):
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(c[token]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(c["muted"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(c["muted"]))
    app.setPalette(palette)
    app.setStyleSheet("""
        QWidget { color: %(text)s; font-family: 'Noto Sans CJK SC', 'Noto Sans', sans-serif; font-size: 13px; }
        QMainWindow, QWidget#root, QStackedWidget, QScrollArea { background: %(base)s; }
        QLabel { background: transparent; border: none; }
        QLabel[role="muted"] { color: %(muted)s; font-size: 12px; line-height: 1.45; }
        QLabel[role="secondary"] { color: %(secondary)s; font-weight: 600; }
        QLabel[role="eyebrow"] { color: %(muted)s; font-size: 11px; font-weight: 600; }
        QLabel[role="title"] { font-size: 23px; font-weight: 700; }
        QLabel[role="section"] { font-size: 16px; font-weight: 600; }
        QLabel[role="brand"] { font-size: 18px; font-weight: 700; }
        QLabel[role="mark"] { background: %(accent)s; color: %(on_accent)s; border-radius: 13px;
                             font-size: 19px; font-weight: 700; }
        QLabel[role="card_icon"] { background: %(tint)s; color: %(accent)s; border-radius: 12px; }
        QLabel[role="empty_mark"] { background: %(tint)s; color: %(accent)s; border-radius: 18px;
                                   font-size: 22px; font-weight: 600; }
        QStackedWidget#transcript_stack { background: transparent; }
        QWidget#sidebar { background: %(shell)s; border-right: 1px solid %(border)s; }
        QWidget#topbar { background: %(base)s; border-bottom: 1px solid %(border)s; }
        QFrame[role="card"] { background: %(surface)s; border: 1px solid %(border)s; border-radius: 22px; }
        QFrame[role="soft"] { background: %(soft)s; border: 1px solid %(border)s; border-radius: 18px; }
        QFrame[role="metric"] { background: %(surface)s; border: 1px solid %(border)s; border-radius: 16px; }
        QFrame[role="error"] { background: %(error_bg)s; border: 1px solid %(error)s; border-radius: 20px; }
        QFrame[role="divider"] { background: %(border)s; border: none; min-height: 1px; max-height: 1px; }
        QLabel[role="badge"] { color: %(accent)s; background: %(tint)s; border-radius: 14px; padding: 5px 14px; font-weight: 600; }
        QLabel[role="badge"][state="active"] { color: %(success)s; background: %(success_bg)s; }
        QLabel[role="badge"][state="error"] { color: %(error)s; background: %(error_bg)s; }
        QPushButton { background: %(surface)s; border: 1px solid %(border)s; border-radius: 16px;
                      padding: 9px 18px; min-height: 20px; font-weight: 600; }
        QPushButton:hover { background: %(hover)s; }
        QPushButton:pressed { background: %(tint)s; }
        QPushButton:focus, QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {
            border: 1px solid %(accent)s; }
        QPushButton[role="primary"] { background: %(accent)s; color: %(on_accent)s; border-color: %(accent)s; font-weight: 600; }
        QPushButton[role="primary"]:hover { background: %(accent_hover)s; border-color: %(accent_hover)s; }
        QPushButton[role="stop"] { color: %(error)s; background: %(error_bg)s; border-color: %(error)s; font-weight: 600; }
        QPushButton[role="quiet"] { background: transparent; border-color: transparent; color: %(accent)s; font-weight: 600; }
        QPushButton[role="quiet"]:hover { background: %(hover)s; }
        QPushButton[role="disclosure"] { background: %(soft)s; border: 1px solid %(border)s; border-radius: 16px;
                                         padding: 11px 16px; text-align: left; color: %(text)s; font-weight: 600; }
        QPushButton[role="disclosure"]:hover { background: %(hover)s; }
        QPushButton[role="disclosure"]:checked { background: %(tint)s; color: %(accent)s; border-color: transparent; }
        QPushButton[role="nav"] { background: transparent; border: 1px solid transparent;
                                  text-align: left; border-radius: 20px; padding: 9px 14px; color: %(secondary)s; font-weight: 500; }
        QPushButton[role="nav"]:hover { background: %(hover)s; }
        QPushButton[role="nav"]:focus { border-color: transparent; }
        QPushButton[role="quiet"]:focus, QPushButton[role="disclosure"]:focus { border-color: %(accent)s; }
        QPushButton[role="nav"]:checked { background: %(tint)s; color: %(accent)s; border-color: transparent; font-weight: 700; }
        QPushButton:disabled { color: %(muted)s; background: %(soft)s; border-color: %(border)s; }
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: %(soft)s; border: 1px solid %(border)s;
                                     border-radius: 14px; padding: 9px 14px; min-height: 20px; }
        QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { background: %(hover)s; }
        QComboBox { padding-right: 26px; }
        QComboBox::drop-down { border: none; width: 24px; }
        QComboBox::down-arrow { image: none; }
        QComboBox QAbstractItemView { background: %(surface)s; color: %(text)s;
                                    selection-background-color: %(tint)s; selection-color: %(text)s;
                                    border: 1px solid %(border)s; border-radius: 12px; padding: 6px; }
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: %(muted)s; }
        QTextEdit { background: transparent; border: 1px solid transparent; border-radius: 12px; padding: 4px; }
        QCheckBox { spacing: 9px; color: %(secondary)s; }
        QCheckBox::indicator { width: 16px; height: 16px; }
        QProgressBar { background: %(soft)s; border: 1px solid %(border)s; border-radius: 8px;
                       min-height: 16px; max-height: 16px; text-align: center; color: %(secondary)s; font-size: 11px; }
        QProgressBar::chunk { background: %(accent)s; border-radius: 7px; }
        QScrollArea { border: none; }
        QScrollBar:vertical { background: transparent; width: 7px; margin: 2px; }
        QScrollBar::handle:vertical { background: %(border)s; border-radius: 3px; min-height: 28px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QSplitter::handle { background: %(base)s; width: 12px; height: 12px; }
        QSplitter::handle:hover { background: %(tint)s; }
        QSplitter::handle:pressed { background: %(accent)s; }
        QTableWidget { background: %(surface)s; border: none; border-radius: 14px; gridline-color: %(border)s; }
        QTableWidget::item { padding: 12px; border-bottom: 1px solid %(border)s; }
        QTableWidget::item:selected { background: %(tint)s; color: %(text)s; }
        QHeaderView::section { background: %(soft)s; color: %(secondary)s; border: none; padding: 12px; text-align: left; font-weight: 600; }
        QToolTip { background: %(surface)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 10px; padding: 6px 10px; }
    """ % c)
    return c
