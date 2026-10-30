import sys
import os
import subprocess
import threading
import time
import ctypes
import math
from pathlib import Path
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPoint, QPointF, QRect
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QTextEdit, QLabel, QSystemTrayIcon,
                             QMenu, QDialog)
from PyQt6.QtGui import (QTextCursor, QIcon, QAction, QPixmap, QPainter,
                         QColor, QBrush, QFont, QPen, QRadialGradient, QRegion)

try:
    import pygetwindow as gw
except ImportError:
    gw = None

try:
    import keyboard
except ImportError:
    keyboard = None

from engine import EveEngine, ASSISTANT_NAME, CAN_UPDATE, IS_FROZEN, APP_DIR


# ==========================================
# HOT RELOADER
# ==========================================
class HotReloader:
    def __init__(self, files, on_change, log_callback=None, check_interval=1.0, debounce=2.5):
        self.files = {}
        self.on_change = on_change
        self.log_callback = log_callback or print
        self.check_interval = check_interval
        self.debounce = debounce
        self._stop = False

        for f in files:
            p = Path(f)
            if p.exists():
                self.files[p] = p.stat().st_mtime
                self.log_callback(f"Слежу за файлом: {p.name}")

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop = True

    def _run(self):
        pending = {}
        while not self._stop:
            time.sleep(self.check_interval)
            for f, old_mtime in list(self.files.items()):
                try:
                    new_mtime = f.stat().st_mtime
                    if new_mtime > old_mtime + 0.5:
                        self.files[f] = new_mtime
                        pending[f] = time.time()
                except FileNotFoundError:
                    continue
            if pending:
                oldest = min(pending.values())
                if time.time() - oldest >= self.debounce:
                    changed = list(pending.keys())
                    pending.clear()
                    valid = True
                    for f in changed:
                        if f.suffix == ".py":
                            try:
                                with open(f, "r", encoding="utf-8") as file:
                                    compile(file.read(), str(f), "exec")
                            except SyntaxError as e:
                                self.log_callback(f"⚠ Ошибка синтаксиса в {f.name}: {e}. Не перезапускаюсь.")
                                valid = False
                    if valid:
                        self.on_change(changed)


# ==========================================
# ШАР
# ==========================================
class EveOrb(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(300, 300)
        self.angle = 0
        self.is_speaking = False
        self.is_listening = False
        self.is_ptt = False
        self.is_night = False
        self.amplitude = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.animate)
        self.timer.start(30)

    def set_states(self, speaking, listening, ptt=False, night=False):
        self.is_speaking = speaking
        self.is_listening = listening
        self.is_ptt = ptt
        self.is_night = night

    def animate(self):
        self.angle += 0.1
        if self.is_speaking:
            self.amplitude = 15 + math.sin(self.angle * 2) * 10
        elif self.is_listening or self.is_ptt:
            self.amplitude = 5 + math.sin(self.angle) * 3
        else:
            self.amplitude = 0.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(float(self.width() / 2.0), float(self.height() / 2.0))

        if self.amplitude > 0:
            for i in range(3):
                radius = 80.0 + (i * 20.0) + self.amplitude
                alpha = int(150 - (i * 40))
                if self.is_speaking:
                    color = QColor(137, 180, 250, alpha)
                elif self.is_ptt:
                    color = QColor(249, 226, 175, alpha)
                else:
                    color = QColor(166, 227, 161, alpha)
                pen = QPen(color, 3)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(center, radius, radius)

        gradient = QRadialGradient(center, 80.0)
        if self.is_speaking:
            gradient.setColorAt(0, QColor(137, 180, 250))
            gradient.setColorAt(1, QColor(30, 30, 46))
        elif self.is_ptt:
            gradient.setColorAt(0, QColor(249, 226, 175))
            gradient.setColorAt(1, QColor(30, 30, 46))
        elif self.is_night:
            gradient.setColorAt(0, QColor(180, 130, 250))
            gradient.setColorAt(1, QColor(20, 15, 35))
        elif self.is_listening:
            gradient.setColorAt(0, QColor(166, 227, 161))
            gradient.setColorAt(1, QColor(30, 30, 46))
        else:
            gradient.setColorAt(0, QColor(88, 91, 112))
            gradient.setColorAt(1, QColor(30, 30, 46))

        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, 80.0, 80.0)

        painter.setPen(QColor(255, 255, 255))
        font = QFont("Segoe UI", 40, QFont.Weight.Bold)
        if self.is_ptt:
            font.setPointSize(20)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "F8")
        else:
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "E")


# ==========================================
# ОКНО ЛОГА
# ==========================================
class LogWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Лог общения")
        self.resize(500, 400)
        self.setStyleSheet("background-color: #1e1e2e; color: #cdd6f4;")
        layout = QVBoxLayout(self)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e2e;
                border: 1px solid #313244;
                border-radius: 10px;
                padding: 10px;
                font-family: 'Segoe UI', sans-serif;
                font-size: 14px;
                color: #cdd6f4;
            }
        """)
        layout.addWidget(self.log)

    def append_text(self, kind, text):
        colors = {"user": "#89b4fa", "eve": "#a6e3a1", "cmd": "#f9e2af",
                  "err": "#f38ba8", "system": "#b4befe"}
        prefix = {"user": "Вы: ", "eve": f"{ASSISTANT_NAME}: ",
                  "cmd": "→ ", "err": "⚠ ", "system": "• "}[kind]
        html = (f'<div style="margin-bottom: 5px;">'
                f'<span style="color:{colors[kind]}; font-weight: bold;">{prefix}</span>'
                f'<span style="color:{colors[kind]};">{text}</span>'
                f'</div>')
        self.log.append(html)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())


# ==========================================
# ГЛАВНОЕ ОКНО
# ==========================================
class EveWindow(QWidget):
    show_signal = pyqtSignal()
    hot_reload_signal = pyqtSignal(list)

    def __init__(self):
        super().__init__()

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setFixedSize(300, 300)

        mask = QRegion(QRect(0, 0, 300, 300), QRegion.RegionType.Ellipse)
        self.setMask(mask)
        self.setStyleSheet("background-color: #1e1e2e;")

        self.old_pos = None

        self.engine = EveEngine()
        self.engine.recognized.connect(self.on_recognized)
        self.engine.spoke.connect(self.on_spoke)
        self.engine.command.connect(self.on_command)
        self.engine.error.connect(self.on_error)
        self.engine.finished.connect(self.on_finished)
        self.engine.open_url.connect(self._open_url)

        self.orb = EveOrb(self)
        self.orb.move(0, 0)

        self.engine.started_speaking.connect(
            lambda: self.orb.set_states(True, False, self.engine.ptt_active, self.engine._is_night_mode()))
        self.engine.finished_speaking.connect(
            lambda: self.orb.set_states(False, self.engine.is_awake, self.engine.ptt_active, self.engine._is_night_mode()))
        self.engine.sleep_mode.connect(self.on_sleep)
        self.engine.ptt_status.connect(self.on_ptt_change)
        self.engine.night_mode_changed.connect(self.on_night_mode)

        self.log_window = LogWindow(self)
        self.setup_tray_icon()
        self.setup_hotkey()

        # HOT RELOAD
        self.hot_reload_signal.connect(self._on_code_changed)
        self.hot_reload_enabled = CAN_UPDATE
        self._setup_hot_reload()

        mode = "exe" if IS_FROZEN else "py"
        reload_status = "hot-reload ВКЛ" if CAN_UPDATE else "hot-reload ВЫКЛ"
        self.on_system_log(f"Ева запущена ({mode}, {reload_status})")
        self.engine.start()
        self.orb.set_states(False, True, False, self.engine._is_night_mode())

    def _setup_hot_reload(self):
        if not CAN_UPDATE:
            self.on_system_log("Hot-reload недоступен (нет исходников рядом с exe).")
            return

        base = APP_DIR
        watched = [base / "engine.py", base / "gui.py"]
        self.reloader = HotReloader(
            files=watched,
            on_change=lambda files: self.hot_reload_signal.emit(files),
            log_callback=lambda msg: self.on_system_log(msg),
            debounce=2.5
        )
        self.reloader.start()

    def _on_code_changed(self, files):
        if not self.hot_reload_enabled:
            return
        if getattr(self.engine, "_self_updating", False):
            self.on_system_log("📝 Изменение от самообновления. Пропускаю hot reload.")
            return
        names = ", ".join([f.name for f in files])
        self.on_system_log(f"📝 Изменён: {names}")
        self.on_system_log("♻ Перезапуск через 1.5 сек...")
        QTimer.singleShot(1500, self._do_restart)

    def _do_restart(self):
        try:
            self.engine.stop()
        except Exception:
            pass
        try:
            subprocess.Popen([sys.executable])
        except Exception as e:
            print(f"Ошибка перезапуска: {e}")
        os._exit(0)

    def _toggle_hot_reload(self, checked):
        self.hot_reload_enabled = checked
        self.on_system_log(f"Авто-перезагрузка: {'ВКЛ' if checked else 'ВЫКЛ'}")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.old_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self.old_pos:
            delta = QPoint(event.globalPosition().toPoint() - self.old_pos)
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.old_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self.old_pos = None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #1e1e2e; color: #cdd6f4; border-radius: 5px; padding: 5px; }
            QMenu::item:selected { background-color: #313244; }
        """)
        action_log = QAction("Показать лог", self)
        action_log.triggered.connect(self.show_log)
        action_reload = QAction("🔄 Перезапустить сейчас", self)
        action_reload.triggered.connect(self._do_restart)
        action_hot = QAction("Авто-перезагрузка", self, checkable=True, checked=self.hot_reload_enabled)
        action_hot.setEnabled(CAN_UPDATE)
        action_hot.triggered.connect(self._toggle_hot_reload)
        action_exit = QAction("Выход", self)
        action_exit.triggered.connect(self.real_exit)
        menu.addAction(action_log)
        menu.addSeparator()
        menu.addAction(action_reload)
        menu.addAction(action_hot)
        menu.addSeparator()
        menu.addAction(action_exit)
        menu.exec(event.globalPos())

    def show_log(self):
        self.log_window.show()
        self.log_window.activateWindow()

    def create_tray_icon_image(self):
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(QColor(137, 180, 250)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(4, 4, 56, 56)
        painter.setPen(QColor(17, 17, 27))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(32)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "E")
        painter.end()
        return QIcon(pixmap)

    def setup_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.create_tray_icon_image())
        self.tray_icon.setToolTip("Eve AI Assistant")
        tray_menu = QMenu()
        tray_menu.setStyleSheet("QMenu { background-color: #1e1e2e; color: #cdd6f4; } QMenu::item:selected { background-color: #313244; }")
        action_show = QAction("Показать Еву", self)
        action_show.triggered.connect(self.show_window)
        action_exit = QAction("Выход", self)
        action_exit.triggered.connect(self.real_exit)
        tray_menu.addAction(action_show)
        tray_menu.addSeparator()
        tray_menu.addAction(action_exit)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
        self.tray_icon.show()

    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window()

    def setup_hotkey(self):
        if keyboard:
            self.show_signal.connect(self.show_window)
            try:
                keyboard.add_hotkey('ctrl+space', self.show_signal.emit)
            except Exception as e:
                print(f"Ошибка хоткея: {e}")

    def show_window(self):
        self.showNormal()
        self.activateWindow()

    def real_exit(self):
        self.engine.stop()
        self.tray_icon.hide()
        QApplication.quit()

    def _open_url(self, url):
        self.on_system_log(f"Открываю: {url}")
        try:
            opera = None
            for p in [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera GX\launcher.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera GX\opera.exe"),
                r"C:\Program Files\Opera GX\launcher.exe",
            ]:
                if os.path.exists(p):
                    opera = p
                    break
            if opera:
                self.showMinimized()
                QApplication.processEvents()
                time.sleep(0.3)
                ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                subprocess.Popen(f'cmd /c start "" "{opera}" "{url}"',
                                 shell=True, startupinfo=startupinfo)
                if gw:
                    threading.Thread(target=self._force_focus_opera, daemon=True).start()
            else:
                os.startfile(url)
                self.showMinimized()
        except Exception as e:
            self.on_error(f"Ошибка: {e}")

    def _force_focus_opera(self):
        for _ in range(6):
            time.sleep(0.5)
            try:
                for win in gw.getAllWindows():
                    if "opera" in win.title.lower():
                        if win.isMinimized:
                            win.restore()
                        win.activate()
            except Exception:
                pass

    def on_recognized(self, t):
        self.log_window.append_text("user", t)
        self.orb.set_states(False, True, self.engine.ptt_active, self.engine._is_night_mode())

    def on_spoke(self, t):
        self.log_window.append_text("eve", t)

    def on_command(self, c):
        self.log_window.append_text("cmd", c)

    def on_system_log(self, t):
        self.log_window.append_text("system", t)

    def on_error(self, e):
        self.log_window.append_text("err", e)
        self.orb.set_states(False, False)

    def on_finished(self):
        self.log_window.append_text("system", "Движок остановлен.")
        self.orb.set_states(False, False)

    def on_sleep(self):
        self.orb.set_states(False, False)
        self.log_window.append_text("system", "Ева спит.")

    def on_ptt_change(self, is_active):
        self.orb.set_states(self.engine.is_speaking, self.engine.is_awake, is_active, self.engine._is_night_mode())

    def on_night_mode(self, is_night):
        self.orb.set_states(self.engine.is_speaking, self.engine.is_awake, self.engine.ptt_active, is_night)
        self.log_window.append_text("system", f"Ночной режим: {'ВКЛ' if is_night else 'ВЫКЛ'}")


if __name__ == "__main__":
    import traceback
    try:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        app.setStyle("Fusion")
        w = EveWindow()
        screen = app.primaryScreen().availableGeometry()
        w.move(screen.center() - w.rect().center())
        w.show()
        sys.exit(app.exec())
    except Exception:
        with open(APP_DIR / "error.log", "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())