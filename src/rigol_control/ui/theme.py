APP_STYLE = """
QWidget {
    color: #e7ebf4;
    font-family: "Noto Sans SC", "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
}
QMainWindow, QDialog, QWidget#AppRoot {
    background: #0c111b;
}
QFrame#Header, QFrame#Panel, QFrame#DeviceCard, QFrame#MetricCard {
    background: #141b28;
    border: 1px solid #263044;
    border-radius: 12px;
}
QFrame#DeviceCard:hover {
    background: #182232;
    border-color: #3a4964;
}
QLabel#Title {
    color: #f4f7ff;
    font-size: 26px;
    font-weight: 700;
}
QLabel#WindowTitle {
    color: #f4f7ff;
    font-size: 22px;
    font-weight: 700;
}
QLabel#CardTitle {
    color: #f4f7ff;
    font-size: 17px;
    font-weight: 650;
}
QLabel#SectionTitle {
    color: #f4f7ff;
    font-size: 16px;
    font-weight: 650;
}
QLabel#Muted {
    color: #8995aa;
}
QLabel#StatusOnline { color: #54d6a3; font-weight: 600; }
QLabel#StatusOffline { color: #ff7d82; font-weight: 600; }
QLabel#StatusPending { color: #f0b95d; font-weight: 600; }
QPushButton {
    background: #202a3b;
    border: 1px solid #334057;
    border-radius: 7px;
    padding: 7px 13px;
    min-height: 20px;
}
QPushButton:hover { background: #2a374c; border-color: #50617e; }
QPushButton:pressed { background: #192232; }
QPushButton:disabled { color: #687387; background: #171d29; border-color: #262e3d; }
QPushButton#PrimaryButton {
    background: #18bcae;
    color: #071513;
    border-color: #24d0c1;
    font-weight: 700;
}
QPushButton#PrimaryButton:hover { background: #22d0bf; }
QPushButton#PrimaryButton:disabled {
    color: #6f7b8e;
    background: #19222e;
    border-color: #2a3545;
}
QPushButton#DangerButton { color: #ff9397; }
QPushButton#NavButton {
    text-align: left;
    border: none;
    border-radius: 8px;
    background: transparent;
    color: #98a5ba;
    padding: 10px 13px;
}
QPushButton#NavButton:hover { background: #1b2535; color: #eef2fa; }
QPushButton#NavButton:checked {
    background: #20333c;
    color: #55ddd0;
    font-weight: 650;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
    background: #101722;
    border: 1px solid #303b50;
    border-radius: 7px;
    padding: 7px 9px;
    selection-background-color: #287d76;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {
    border-color: #20bbae;
}
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QTableWidget {
    background: #111823;
    alternate-background-color: #141e2c;
    border: 1px solid #2a3549;
    border-radius: 8px;
    gridline-color: #283347;
    selection-background-color: #245a5a;
}
QHeaderView::section {
    background: #1a2433;
    color: #b9c3d3;
    border: none;
    border-right: 1px solid #2b3649;
    padding: 8px;
}
QTabWidget::pane { border: 1px solid #2a3549; border-radius: 8px; top: -1px; }
QTabBar::tab { background: #151d2a; color: #929eb1; padding: 9px 15px; }
QTabBar::tab:selected { background: #20333c; color: #55ddd0; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: #101722; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #344157; border-radius: 5px; min-height: 30px; }
QToolTip { background: #202a3a; color: #fff; border: 1px solid #44526a; }
"""
