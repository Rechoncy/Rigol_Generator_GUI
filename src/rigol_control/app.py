from __future__ import annotations

import argparse
import atexit
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette
from PyQt6.QtWidgets import QApplication

from .repository import DeviceRepository
from .installation import acquire_application_mutex
from .runtime import package_resource, prepare_data_dir, prepare_qt_environment
from .ui.main_window import DeviceCenterWindow
from .ui.input_behavior import InputWheelGuard
from .ui.theme import APP_STYLE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DG4000、Vaunix LMS/BLX 与 NI DAQ 多设备控制中心"
    )
    parser.add_argument("--data-dir", type=Path, help="覆盖设备配置保存目录")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="启动界面后自动退出，用于无交互环境验证",
    )
    return parser


def create_application(argv: list[str] | None = None) -> QApplication:
    prepare_qt_environment()
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "RigolGeneratorControl.LabControlCenter"
        )
    app = QApplication(argv if argv is not None else sys.argv)
    app.setWindowIcon(QIcon(str(package_resource("assets", "app_icon.ico"))))
    app.setApplicationName("DG4000 Vaunix NI Control Center")
    app.setOrganizationName("RigolGeneratorControl")
    app.setStyle("Fusion")
    font_family = load_ui_font()
    if font_family:
        app.setFont(QFont(font_family, 10))
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#0c111b"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e7ebf4"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#101722"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e7ebf4"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#202a3b"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e7ebf4"))
    app.setPalette(palette)
    app.setStyleSheet(APP_STYLE)
    app._input_wheel_guard = InputWheelGuard(app)
    app.installEventFilter(app._input_wheel_guard)
    # Remove Python callbacks before Qt tears down during interpreter exit.
    # Retaining these bound arguments also keeps the filter alive for the app.
    atexit.register(app.removeEventFilter, app._input_wheel_guard)
    return app


def load_ui_font() -> str | None:
    """Load a CJK font explicitly when Qt's platform plugin omits system fonts."""

    candidates = (
        Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    )
    for path in candidates:
        if not path.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                return families[0]
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    acquire_application_mutex()
    data_dir = prepare_data_dir(args.data_dir)
    # All per-device default recording paths must follow --data-dir as well.
    os.environ["RIGOL_GUI_DATA_DIR"] = str(data_dir)
    qt_argv = [sys.argv[0]]
    app = create_application(qt_argv)
    repository = DeviceRepository(data_dir)
    window = DeviceCenterWindow(repository)
    window.show()
    if args.smoke_test:
        QTimer.singleShot(250, app.quit)
    else:
        window.update_panel.start_automatic_checks()
    return app.exec()
