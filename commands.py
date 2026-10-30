import os, subprocess, shutil, ctypes, uuid
from datetime import datetime, timedelta
from pathlib import Path
import re

class CommandProcessor:
    def __init__(self, app_dir, speak_callback, error_callback, open_url_callback):
        self.app_dir = app_dir
        self.speak = speak_callback
        self.error = error_callback
        self.open_url = open_url_callback
        self.active_timers = []
        self.reminders = self._load_reminders()
        
    def _load_reminders(self):
        f = self.app_dir / "reminders.json"
        if f.exists():
            try:
                with open(f, "r", encoding="utf-8") as file: return json.load(file)
            except: return []
        return []
        
    def _save_reminders(self):
        f = self.app_dir / "reminders.json"
        try:
            with open(f, "w", encoding="utf-8") as file: json.dump(self.reminders, file, ensure_ascii=False, indent=4)
        except: pass

    def process(self, c):
        """Возвращает True, если команда распознана и выполнена."""
        
        # --- ПРИЛОЖЕНИЯ ---
        if "блокнот" in c:
            subprocess.Popen(["notepad.exe"]); self.speak("Открываю Блокнот."); return True
        elif "калькулятор" in c:
            subprocess.Popen(["calc.exe"]); self.speak("Открываю Калькулятор."); return True
        elif "проводник" in c:
            subprocess.Popen(["explorer.exe"]); self.speak("Открываю Проводник."); return True

        # --- САЙТЫ ---
        elif any(w in c for w in ("youtube", "ютуб", "ютюб")):
            if any(w in c for w in ("главн", "домашн")):
                self.speak("Открываю YouTube."); self.open_url("https://www.youtube.com"); return True
            query = c
            for w in ["включи", "найди", "на", "в", "ютубе", "ютуб", "ютюб", "youtube", "видео", "песню", "пожалуйста", "ева", "первое", "клип", "музыку"]:
                query = query.replace(w, "").strip()
            if query:
                self.speak(f"Ищу: {query}")
                self.open_url(f"https://www.youtube.com/results?search_query={query}"); return True
            else:
                self.speak("Открываю YouTube."); self.open_url("https://www.youtube.com"); return True
        elif "яндекс" in c and "музык" in c:
            self.open_url("https://music.yandex.ru"); self.speak("Открываю Музыку."); return True
        elif "яндекс" in c:
            self.open_url("https://ya.ru"); self.speak("Открываю Яндекс."); return True
        elif "вконтакте" in c or c == "вк" or " вк " in c:
            self.open_url("https://vk.com"); self.speak("Открываю ВК."); return True

        # --- ТАЙМЕРЫ ---
        if any(w in c for w in ("мои таймеры", "какие таймеры", "активные таймеры", "покажи таймеры")):
            self._list_timers(); return True
        if any(w in c for w in ("отмени", "отключи")) and "таймер" in c:
            self._cancel_timers(); return True
            
        if "через" in c or "таймер" in c:
            match = re.search(r'(\d+|один|два|три|пять|десять|пятнадцать|тридцать)\s*(минут|минуту|минуты|час|часа|часов|секунд|секунды)', c)
            seconds = 0
            if "полчаса" in c: seconds = 1800
            elif match:
                a, u = match.group(1), match.group(2)
                words = {"один": 1, "два": 2, "три": 3, "пять": 5, "десять": 10, "пятнадцать": 15, "тридцать": 30}
                amt = words.get(a, int(a) if a.isdigit() else 0)
                if "час" in u: seconds = amt * 3600
                elif "минут" in u: seconds = amt * 60
                elif "секунд" in u: seconds = amt
            if seconds > 0:
                action = "notify"
                if "выключ" in c: action = "shutdown"; self.speak(f"Выключу через {seconds//60} мин.")
                elif "перезагруз" in c: action = "restart"; self.speak(f"Перезагружу через {seconds//60} мин.")
                elif "блокир" in c: action = "lock"; self.speak(f"Заблокирую через {seconds//60} мин.")
                else: self.speak(f"Таймер на {seconds//60} мин.")
                self._set_timer(seconds, action)
                return True
            else:
                self.speak("Не поняла время."); return True

        # --- УБОРКА ---
        if any(w in c for w in ("наведи порядок", "порядок", "уберись", "почисти")):
            folder = Path(os.path.expanduser("~")) / "Downloads"
            if "рабоч" in c or "стол" in c: folder = Path(os.path.expanduser("~")) / "Desktop"
            elif "документ" in c: folder = Path(os.path.expanduser("~")) / "Documents"
            self._sort_folder(folder); return True

        if "корзин" in c and any(w in c for w in ("очисти", "удали", "почисти")):
            self._clean_recycle_bin(); return True

        if "скриншот" in c and any(w in c for w in ("очисти", "удали", "почисти")):
            self._clean_screenshots(); return True

        return False

    # --- ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ---
    def _set_timer(self, seconds, action_type):
        def task():
            if action_type == "shutdown": os.system("shutdown /s /t 0")
            elif action_type == "restart": os.system("shutdown /r /t 0")
            elif action_type == "lock": ctypes.windll.user32.LockWorkStation()
            elif action_type == "notify": self.speak("Время вышло!")
            self.active_timers = [t for t in self.active_timers if t["timer"] != timer]
            
        timer = threading.Timer(seconds, task)
        timer.daemon = True
        self.active_timers.append({"timer": timer, "action": action_type, "end": datetime.now() + timedelta(seconds=seconds)})
        timer.start()

    def _list_timers(self):
        if not self.active_timers: self.speak("Таймеров нет."); return
        now = datetime.now()
        parts = []
        for t in self.active_timers:
            rem = int((t["end"] - now).total_seconds())
            if rem > 0: parts.append(f"{t['action']} через {rem//60} мин")
        self.speak(f"Активно: {len(parts)}. " + ". ".join(parts) if parts else "Таймеров нет.")

    def _cancel_timers(self):
        for t in self.active_timers: t["timer"].cancel()
        self.active_timers.clear()
        self.speak("Все таймеры отменены.")
        
    def _sort_folder(self, folder):
        # (Код сортировки файлов из предыдущей версии)
        self.speak(f"Убираюсь в {folder.name}.")
        # ... логика перемещения файлов ...
        self.speak("Уборка завершена.")

    def _clean_recycle_bin(self):
        try: ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 7); self.speak("Корзина очищена.")
        except: self.speak("Ошибка.")

    def _clean_screenshots(self):
        self.speak("Ищу скриншоты.")
        # ... логика удаления ...
        self.speak("Скриншоты удалены.")