import json, queue, subprocess, sys, threading, time, os, ctypes, io, base64
import requests
import pyautogui
import cv2
import pyperclip
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
import importlib
import importlib.util
import re
import shutil

from duckduckgo_search import DDGS
import wikipedia
from bs4 import BeautifulSoup

import sounddevice as sd
from vosk import Model, KaldiRecognizer
from PyQt6.QtCore import QObject, pyqtSignal

import pygame
from gtts import gTTS

try:
    import keyboard
except ImportError:
    keyboard = None

# ============================================
# ОПРЕДЕЛЕНИЕ ПУТЕЙ (гибрид exe + .py)
# ============================================
IS_FROZEN = getattr(sys, "frozen", False)

if IS_FROZEN:
    APP_DIR = Path(sys.executable).parent
    BUNDLE_DIR = Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else APP_DIR
else:
    APP_DIR = Path(__file__).parent
    BUNDLE_DIR = Path(__file__).parent

_engine_file = APP_DIR / "engine.py"
CAN_UPDATE = _engine_file.exists() and os.access(str(_engine_file), os.W_OK)

ASSISTANT_NAME = "Ева"
MODEL_PATH = BUNDLE_DIR / "model"
if not MODEL_PATH.exists():
    MODEL_PATH = APP_DIR / "model"

SKILLS_DIR = APP_DIR / "skills"
BACKUPS_DIR = APP_DIR / "backups"
SAMPLE_RATE = 16000
BLOCK_SIZE = 4000
DUPLICATE_WINDOW_SECONDS = 2.0
MEMORY_FILE = APP_DIR / "memory.json"
REMINDERS_FILE = APP_DIR / "reminders.json"

PROXYAPI_KEY = "sk-TnT1lVru6vZF3JvU2eXBNrxmXdA529iy"

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002


def send_media_key(vk_code):
    try:
        ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY, 0)
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        return False


class EveEngine(QObject):
    recognized = pyqtSignal(str)
    spoke = pyqtSignal(str)
    command = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal()
    open_url = pyqtSignal(str)
    started_speaking = pyqtSignal()
    finished_speaking = pyqtSignal()
    sleep_mode = pyqtSignal()
    ptt_status = pyqtSignal(bool)
    night_mode_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._speech_lock = threading.Lock()
        self.is_speaking = False
        self.is_awake = False
        self.last_wake_time = 0.0
        self.active_timers = []
        self.dictation_mode = False
        self.ptt_active = False
        self._night_mode_override = None
        self.skills = []
        self.start_time = time.time()
        self.reminders = self._load_reminders()
        self._self_updating = False
        pygame.mixer.init()

        self.memories = self._load_memory()
        self.chat_history = []
        self._update_system_prompt()

        self._load_skills()
        self.setup_ptt_hotkey()
        threading.Thread(target=self._reminder_loop, daemon=True).start()

    # ============ САМООБНОВЛЕНИЕ ============

    def _make_backup(self, file_path):
        BACKUPS_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = BACKUPS_DIR / f"{file_path.stem}_{timestamp}.py"
        shutil.copy2(file_path, backup_file)
        return backup_file

    def _self_update(self, request):
        if not CAN_UPDATE:
            self._speak("Самообновление недоступно. Нужен engine.py рядом с exe.")
            return
        self._speak("Делаю резервную копию и отправляю запрос нейросети.")
        try:
            engine_file = APP_DIR / "engine.py"
            with open(engine_file, "r", encoding="utf-8") as f:
                current_code = f.read()

            backup = self._make_backup(engine_file)
            print(f"[БЭКАП] {backup.name}")

            self._self_updating = True

            prompt = (
                "Ты — Python-разработчик, который обновляет код голосового ассистента Евы.\n\n"
                "Вот ПОЛНЫЙ текущий код engine.py:\n\n"
                f"{current_code}\n\n"
                f"=== ЗАПРОС ПОЛЬЗОВАТЕЛЯ ===\n{request}\n\n"
                "=== ТВОИ ЗАДАЧИ ===\n"
                "1. Внеси запрошенные изменения.\n"
                "2. Верни ПОЛНЫЙ код файла (все строки, не фрагмент).\n"
                "3. СОХРАНИ все существующие классы, методы, сигналы, импорты.\n"
                "4. Соблюдай отступы в 4 пробела.\n"
                "5. БЕЗ markdown, без ```python, без объяснений.\n"
                "6. Начни ответ со слова 'import' или '#'.\n"
                "7. Ответ — ТОЛЬКО код.\n"
            )

            headers = {"Authorization": f"Bearer {PROXYAPI_KEY}", "Content-Type": "application/json"}
            data = {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 16000,
            }
            response = requests.post("https://api.proxyapi.ru/openai/v1/chat/completions",
                                     headers=headers, json=data, timeout=180)

            if response.status_code != 200:
                self._self_updating = False
                self._speak(f"Ошибка API: {response.status_code}.")
                return

            choice = response.json()["choices"][0]
            new_code = choice["message"]["content"].strip()
            finish_reason = choice.get("finish_reason", "")

            if new_code.startswith("```"):
                lines = new_code.split("\n", 1)
                new_code = lines[1] if len(lines) > 1 else new_code[3:]
            if new_code.endswith("```"):
                new_code = new_code[:-3]
            new_code = new_code.strip()

            if finish_reason == "length":
                self._self_updating = False
                self._speak("Ответ нейросети обрезан. Откатываю.")
                return

            if not new_code.startswith(("import", "from", "#")):
                self._self_updating = False
                self._speak("Нейросеть вернула что-то странное. Откатываю.")
                return

            try:
                compile(new_code, "engine.py", "exec")
            except SyntaxError as e:
                self._self_updating = False
                self._speak(f"Ошибка синтаксиса в строке {e.lineno}. Откатываю.")
                self.error.emit(f"SyntaxError: {e}")
                return

            required = ["class EveEngine", "def _handle_command", "def _speak", "ASSISTANT_NAME"]
            for req in required:
                if req not in new_code:
                    self._self_updating = False
                    self._speak(f"Нейросеть потеряла кусок ({req}). Откатываю.")
                    return

            with open(engine_file, "w", encoding="utf-8") as f:
                f.write(new_code)

            print(f"[SELF-UPDATE] engine.py обновлён")
            self._speak("Обновление готово. Перезапускаюсь через 3 секунды.")
            time.sleep(3)
            subprocess.Popen([sys.executable])
            os._exit(0)

        except Exception as e:
            self._self_updating = False
            self._speak("Ошибка самообновления.")
            self.error.emit(f"self_update: {e}")

    def _rollback(self):
        if not CAN_UPDATE:
            self._speak("Откат недоступен. Нужен engine.py рядом с exe.")
            return
        try:
            if not BACKUPS_DIR.exists():
                self._speak("Бэкапов нет.")
                return
            backups = sorted(BACKUPS_DIR.glob("engine_*.py"), reverse=True)
            if not backups:
                self._speak("Бэкапов нет.")
                return
            latest = backups[0]
            engine_file = APP_DIR / "engine.py"
            self._make_backup(engine_file)
            shutil.copy2(latest, engine_file)
            self._speak(f"Откатилась к {latest.name}. Перезапускаюсь.")
            time.sleep(3)
            subprocess.Popen([sys.executable])
            os._exit(0)
        except Exception as e:
            self._speak("Ошибка отката.")
            self.error.emit(f"rollback: {e}")

    def _list_backups(self):
        if not BACKUPS_DIR.exists():
            self._speak("Бэкапов нет.")
            return
        backups = sorted(BACKUPS_DIR.glob("engine_*.py"), reverse=True)
        if not backups:
            self._speak("Бэкапов нет.")
            return
        self._speak(f"У меня {len(backups)} бэкапов. Последние: {', '.join([b.name for b in backups[:3]])}")

    # ============ НАПОМИНАНИЯ ============

    def _load_reminders(self):
        if REMINDERS_FILE.exists():
            try:
                with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_reminders(self):
        try:
            with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.reminders, f, ensure_ascii=False, indent=4)
        except Exception as e:
            self.error.emit(f"Ошибка: {e}")

    def _reminder_loop(self):
        while not self._stop.is_set():
            try:
                now = datetime.now()
                to_remove = []
                for r in self.reminders:
                    try:
                        rt = datetime.strptime(r["time"], "%Y-%m-%d %H:%M")
                        if now >= rt and not r.get("fired", False):
                            self._speak(f"Напоминание: {r['text']}")
                            r["fired"] = True
                            to_remove.append(r)
                    except Exception:
                        pass
                if to_remove:
                    for r in to_remove:
                        if r in self.reminders:
                            self.reminders.remove(r)
                    self._save_reminders()
            except Exception:
                pass
            time.sleep(30)

    def _add_reminder(self, text, time_str):
        self.reminders.append({"text": text, "time": time_str, "fired": False})
        self._save_reminders()

    def _list_reminders(self, filter_date=None):
        active = [r for r in self.reminders if not r.get("fired", False)]
        if filter_date:
            active = [r for r in active if r["time"].startswith(filter_date)]
        return active

    # ============ НАВЫКИ ============

    def _load_skills(self):
        self.skills = []
        if not SKILLS_DIR.exists():
            SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            return

        for py_file in sorted(SKILLS_DIR.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"skill_{py_file.stem}", py_file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if not hasattr(module, "handle"):
                    continue
                self.skills.append({
                    "name": getattr(module, "NAME", py_file.stem),
                    "desc": getattr(module, "DESCRIPTION", "Без описания"),
                    "module": module,
                })
            except Exception as e:
                print(f"[X] Навык {py_file.name}: {e}")

    def _try_skills(self, command):
        for skill in self.skills:
            try:
                result = skill["module"].handle(command)
                if result is True:
                    return True
                if isinstance(result, str) and result.strip():
                    self._speak(result)
                    return True
            except Exception:
                pass
        return False

    # ============ НОЧНОЙ РЕЖИМ ============

    def _is_night_mode(self):
        if self._night_mode_override is not None:
            return self._night_mode_override
        hour = datetime.now().hour
        return hour >= 23 or hour < 7

    def _whisper_volume(self):
        return 0.22 if self._is_night_mode() else 1.0

    def _can_open_window(self, name="это"):
        if self._is_night_mode():
            self._speak(f"Ночной режим. Не буду открывать {name}.")
            return False
        return True

    # ============ PTT ============

    def setup_ptt_hotkey(self):
        if keyboard:
            try:
                keyboard.on_press_key('f8', self._on_ptt_press)
                keyboard.on_release_key('f8', self._on_ptt_release)
            except Exception:
                pass

    def _on_ptt_press(self, event):
        if not self.ptt_active:
            self.ptt_active = True
            self.is_awake = True
            self.ptt_status.emit(True)
            if keyboard:
                for _ in range(15):
                    keyboard.send('volume down')

    def _on_ptt_release(self, event):
        if self.ptt_active:
            self.ptt_active = False
            self.ptt_status.emit(False)
            if keyboard:
                for _ in range(15):
                    keyboard.send('volume up')

    # ============ ПАМЯТЬ ============

    def _load_memory(self):
        if MEMORY_FILE.exists():
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_memory(self):
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.memories, f, ensure_ascii=False, indent=4)
        except Exception as e:
            self.error.emit(f"Ошибка: {e}")

    def _update_system_prompt(self):
        base = ("Ты — голосовой ассистент Ева. Отвечай кратко, дружелюбно, на русском языке. "
                "Твой ответ будет озвучен голосом, поэтому никогда не используй маркдаун, смайлики, "
                "списки и сложное форматирование. Пиши только чистый текст.")
        if self.memories:
            base += " Вот что ты знаешь о пользователе: " + "; ".join(self.memories) + "."
        if not self.chat_history:
            self.chat_history = [{"role": "system", "content": base}]
        else:
            self.chat_history[0]["content"] = base

    # ============ ЖИЗНЕННЫЙ ЦИКЛ ============

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass

    # ============ ОЗВУЧКА ============

    def _speak(self, text):
        self.spoke.emit(text)
        if self._stop.is_set():
            return
        threading.Thread(target=self._speak_gtts, args=(text,), daemon=True).start()

    def _speak_gtts(self, text):
        with self._speech_lock:
            self.is_speaking = True
            self.started_speaking.emit()
            temp_file = APP_DIR / f"eve_temp_{int(time.time()*1000)}.mp3"
            try:
                tts = gTTS(text=text, lang='ru')
                tts.save(str(temp_file))
                pygame.mixer.music.load(str(temp_file))
                pygame.mixer.music.set_volume(self._whisper_volume())
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy() and not self._stop.is_set():
                    pygame.time.Clock().tick(10)
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
                try:
                    os.remove(temp_file)
                except Exception:
                    pass
            except Exception as e:
                self.error.emit(f"Ошибка gTTS: {e}")
                self._speak_sapi5(text)
            finally:
                time.sleep(1.0)
                self.is_speaking = False
                self.finished_speaking.emit()

    def _speak_sapi5(self, text):
        code = r"""
import sys, pyttsx3
text = sys.argv[1]
e = pyttsx3.init("sapi5")
e.setProperty("rate", 165); e.setProperty("volume", 1.0)
for v in e.getProperty("voices"):
    if any(w in (v.name + v.id).lower() for w in ("russian","русский","ru-ru")):
        e.setProperty("voice", v.id); break
e.say(text); e.runAndWait(); e.stop()
"""
        try:
            subprocess.run([sys.executable, "-c", code, text],
                           capture_output=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass

    # ============ РАСПОЗНАВАНИЕ ============

    def _listen_once(self, model):
        q = queue.Queue()
        rec = KaldiRecognizer(model, SAMPLE_RATE)

        def cb(indata, frames, t, status):
            q.put(bytes(indata))

        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                               dtype="int16", channels=1, callback=cb):
            while not self._stop.is_set():
                try:
                    data = q.get(timeout=0.3)
                except queue.Empty:
                    continue
                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result()).get("text", "").lower().strip()
                    if text:
                        return text
        return None

    def _run(self):
        try:
            if not MODEL_PATH.exists():
                self.error.emit(f"Нет модели: {MODEL_PATH}")
                return
            self.night_mode_changed.emit(self._is_night_mode())
            hour = datetime.now().hour
            if 5 <= hour < 12:
                greeting = "Доброе утро"
            elif 12 <= hour < 18:
                greeting = "Добрый день"
            else:
                greeting = "Добрый вечер"
            if self._is_night_mode():
                self._speak("Доброй ночи. Ночной режим.")
            else:
                self._speak(f"{greeting}. Я Ева. Скажи мое имя.")
            model = Model(str(MODEL_PATH))
            last_text, last_time = None, 0.0
            while not self._stop.is_set():
                if self.is_speaking:
                    time.sleep(0.1)
                    continue
                text = self._listen_once(model)
                if not text or self._stop.is_set() or self.is_speaking:
                    continue
                self.recognized.emit(text)
                now = time.monotonic()
                if text == last_text and now - last_time < DUPLICATE_WINDOW_SECONDS:
                    continue
                last_text, last_time = text, now
                self._process_wake_word(text)
        except Exception as e:
            import traceback
            self.error.emit(traceback.format_exc())
        finally:
            self.finished.emit()

    def _type_text(self, text):
        if not text.strip():
            return
        pyperclip.copy(text + " ")
        time.sleep(0.1)
        try:
            if keyboard:
                keyboard.send('ctrl+v')
            else:
                pyautogui.hotkey('ctrl', 'v')
        except Exception as e:
            self.error.emit(f"Ошибка: {e}")

    def _process_wake_word(self, text):
        if self.dictation_mode:
            if "стоп" in text or "хватит" in text:
                if "диктов" in text or "печат" in text or "ева" in text:
                    self.dictation_mode = False
                    self._speak("Диктовка выключена.")
                    return
            self._type_text(text)
            return

        now = time.time()
        if self.is_awake and (now - self.last_wake_time > 10.0) and not self.ptt_active:
            self.is_awake = False
            self.sleep_mode.emit()

        wake_words = ["ева", "еве", "еву", "eve", "эва", "лева", "нева"]
        wake_detected = False
        for word in wake_words:
            if word in text:
                wake_detected = True
                text = text.replace(word, "").strip()
                break

        if wake_detected:
            self.is_awake = True
            self.last_wake_time = now
            if not text:
                self._speak("Слушаю.")
                return

        if self.is_awake:
            self.last_wake_time = now
            self.command.emit(text)
            self._handle_command(text)

    # ============ ТАЙМЕРЫ ============

    def _set_timer(self, seconds, action_type):
        def task():
            if action_type == "shutdown":
                os.system("shutdown /s /t 0")
            elif action_type == "restart":
                os.system("shutdown /r /t 0")
            elif action_type == "lock":
                ctypes.windll.user32.LockWorkStation()
            elif action_type == "notify":
                self._speak("Время вышло!")
            if timer in self.active_timers:
                self.active_timers.remove(timer)

        timer = threading.Timer(seconds, task)
        timer.daemon = True
        timer.start()
        self.active_timers.append(timer)

    # ============ ДИАГНОСТИКА ============

    def _self_diagnose(self):
        self._speak("Запускаю диагностику.")
        errors_list = []
        if not (MODEL_PATH.exists() and (MODEL_PATH / "am").exists()):
            errors_list.append("модель")
        try:
            import sounddevice, vosk, pygame, gtts, requests
            import pygetwindow, pyperclip, pyautogui, cv2
        except Exception:
            errors_list.append("библиотеки")
        try:
            sf = APP_DIR / "custom_skills.py"
            if sf.exists():
                with open(sf, "r", encoding="utf-8") as f:
                    compile(f.read(), str(sf), "exec")
        except SyntaxError:
            errors_list.append("custom_skills")
        try:
            headers = {"Authorization": f"Bearer {PROXYAPI_KEY}", "Content-Type": "application/json"}
            data = {"model": "gpt-4o", "messages": [{"role": "user", "content": "тест"}], "max_tokens": 5}
            r = requests.post("https://api.proxyapi.ru/openai/v1/chat/completions",
                              headers=headers, json=data, timeout=10)
            if r.status_code != 200:
                errors_list.append(f"API {r.status_code}")
        except Exception:
            errors_list.append("интернет")
        try:
            d = sd.query_devices()
            if not [x for x in d if x['max_input_channels'] > 0]:
                errors_list.append("микрофон")
        except Exception:
            errors_list.append("микрофон")
        if not errors_list:
            self._speak("Все системы в норме.")
        else:
            self._speak(f"Проблемы: {', '.join(errors_list)}.")

    def _suggest_improvements(self):
        self._speak("Анализирую свои возможности. Секунду.")
        info = "Текущие возможности:\n- Базовые: время, погода, таймеры, напоминания, программы, сайты, поиск, музыка, громкость, память, буфер, диктовка, зрение, диагностика, самообучение, ночной режим, режим рации F8.\n"
        if self.skills:
            info += f"- Навыки: {', '.join([s['name'] for s in self.skills])}.\n"
        if self.memories:
            info += f"- Память: {'; '.join(self.memories)}.\n"
        prompt = (
            "Ты — эксперт по UX. Вот что умеет ассистент Ева:\n"
            + info
            + "\nПредложи РОВНО 3 конкретных улучшения (до 15 слов каждое). "
              "Не повторяй уже существующее. Без markdown. Формат: 'Первое. ... Второе. ... Третье. ...'"
        )
        headers = {"Authorization": f"Bearer {PROXYAPI_KEY}", "Content-Type": "application/json"}
        data = {"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.8, "max_tokens": 250}
        try:
            r = requests.post("https://api.proxyapi.ru/openai/v1/chat/completions",
                              headers=headers, json=data, timeout=30)
            answer = r.json()["choices"][0]["message"]["content"]
            self._speak(answer)
        except Exception as e:
            self._speak("Ошибка анализа.")
            self.error.emit(f"Ошибка: {e}")

    def _list_abilities(self):
        uptime = int(time.time() - self.start_time)
        h = uptime // 3600
        m = (uptime % 3600) // 60
        text = (f"Работаю уже {h} часов {m} минут. Умею: время, погоду, таймеры, напоминания, "
                f"программы, сайты, поиск, музыку, память, буфер, диктовку, зрение, диагностику, "
                f"самообучение, самообновление.")
        if self.skills:
            text += f" Навыки: {', '.join([s['name'] for s in self.skills])}."
        if self.memories:
            text += f" Помню {len(self.memories)} фактов о тебе."
        self._speak(text)

    # ============ ПОИСК / ОБУЧЕНИЕ / LLM ============

    def _search_internet(self, query):
        self._speak("Ищу.")
        context = ""
        try:
            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=7)
            soup = BeautifulSoup(r.text, 'html.parser')
            results = [x.get_text(strip=True) for x in soup.find_all('a', class_='result__snippet', limit=3)]
            if results:
                context = "Данные:\n" + "\n".join([f"- {x}" for x in results])
        except Exception:
            pass
        if not context:
            try:
                context = f"Википедия: {wikipedia.summary(query)}"
            except Exception:
                pass
        prompt = f"Ответь: '{query}'. Данные:\n{context}" if context else f"Ответь: '{query}'."
        self._ask_llm(prompt)

    def _learn_new_skill(self, request):
        self._speak("Пишу код.")
        prompt = (
            "Ты — Python-разработчик. Напиши код ДЛЯ ВСТАВКИ ВНУТРЬ функции.\n"
            f"Запрос: {request}\n"
            "ПРАВИЛА: НЕ пиши def. Используй subprocess, webbrowser, os. Верни True.\n"
            "ТОЛЬКО код, отступы 4 пробела."
        )
        headers = {"Authorization": f"Bearer {PROXYAPI_KEY}", "Content-Type": "application/json"}
        data = {"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
        try:
            r = requests.post("https://api.proxyapi.ru/openai/v1/chat/completions",
                              headers=headers, json=data, timeout=30)
            new_code = r.json()["choices"][0]["message"]["content"].strip()
            new_code = new_code.replace("```python", "").replace("```", "").strip()

            sf = APP_DIR / "custom_skills.py"
            if not sf.exists():
                with open(sf, "w", encoding="utf-8") as f:
                    f.write('''import subprocess
import webbrowser
import os

def handle_custom_command(c):
    # === НАЧАЛО НАВЫКОВ ЕВЫ ===
    
    # === КОНЕЦ НАВЫКОВ ЕВЫ ===
    return False
''')

            with open(sf, "r", encoding="utf-8") as f:
                content = f.read()
            marker = "# === КОНЕЦ НАВЫКОВ ЕВЫ ==="
            if marker in content:
                indented = "\n".join("    " + line if line.strip() else line
                                     for line in new_code.split("\n"))
                updated = content.replace(marker, f"{indented}\n\n    {marker}")
                try:
                    compile(updated, str(sf), "exec")
                except SyntaxError:
                    self._speak("Ошибка в коде. Отменяю.")
                    return
                with open(sf, "w", encoding="utf-8") as f:
                    f.write(updated)
                self._speak("Навык добавлен. Перезапускаюсь.")
                time.sleep(3)
                subprocess.Popen([sys.executable])
                os._exit(0)
        except Exception as e:
            self._speak("Ошибка.")
            self.error.emit(f"Ошибка: {e}")

    def _ask_llm(self, text, image_base64=None):
        try:
            content = [{"type": "text", "text": text}]
            if image_base64:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                })
            self.chat_history.append({"role": "user", "content": text})
            messages = self.chat_history[:-1] + [{"role": "user", "content": content}]
            headers = {"Authorization": f"Bearer {PROXYAPI_KEY}", "Content-Type": "application/json"}
            data = {"model": "gpt-4o", "messages": messages, "max_tokens": 400, "temperature": 0.7}
            r = requests.post("https://api.proxyapi.ru/openai/v1/chat/completions",
                              headers=headers, json=data, timeout=60)
            if r.status_code != 200:
                self.error.emit(f"API {r.status_code}")
                return
            answer = r.json()["choices"][0]["message"]["content"]
            self.chat_history.append({"role": "assistant", "content": answer})
            if len(self.chat_history) > 11:
                self.chat_history = [self.chat_history[0]] + self.chat_history[-10:]
            self._speak(answer)
        except Exception as e:
            self.error.emit(f"Ошибка: {str(e)}")

    # ============ ГЛАВНЫЙ ОБРАБОТЧИК ============

    def _handle_command(self, command):
        c = command.lower().strip()
        say = self._speak
        if not c or len(c) < 3:
            return True

        # САМООБНОВЛЕНИЕ
        update_triggers = ["обнови себя", "улучши себя", "перепиши свой код",
                           "добавь в себя", "модифицируй себя", "измени свой код"]
        if any(t in c for t in update_triggers):
            if not CAN_UPDATE:
                say("Самообновление недоступно. Нужен engine.py рядом с exe.")
                return True
            request = c
            for t in update_triggers:
                request = request.replace(t, "")
            request = request.strip(" :,-")
            if not request:
                say("Что именно добавить или изменить в моём коде?")
            else:
                self._self_update(request)
            return True

        if "откати себя" in c or "восстанови себя" in c:
            self._rollback()
            return True

        if "покажи бэкапы" in c or "список бэкапов" in c:
            self._list_backups()
            return True

        # НАПОМИНАНИЯ
        if "напомни" in c:
            now = datetime.now()
            remind_time = None
            text_to_save = c.replace("напомни", "").replace("мне", "").strip()
            time_phrase = ""
            if "завтра" in c:
                remind_time = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0)
                time_phrase = "завтра"
                tm = re.search(r'в\s+(\d{1,2})[:\.](\d{2})', c)
                if tm:
                    remind_time = remind_time.replace(hour=int(tm.group(1)), minute=int(tm.group(2)))
                    time_phrase += f" в {tm.group(1)}:{tm.group(2)}"
            elif "сегодня" in c:
                remind_time = now.replace(second=0, microsecond=0)
                time_phrase = "сегодня"
                tm = re.search(r'в\s+(\d{1,2})[:\.](\d{2})', c)
                if tm:
                    remind_time = remind_time.replace(hour=int(tm.group(1)), minute=int(tm.group(2)))
                    time_phrase += f" в {tm.group(1)}:{tm.group(2)}"
            elif "через" in c:
                match = re.search(
                    r'через\s+(\d+|один|одну|два|две|три|пять|десять|пятнадцать|тридцать|полчаса|час)\s*(минут|минуту|минуты|час|часа|часов)',
                    c)
                if match:
                    a = match.group(1)
                    u = match.group(2)
                    words = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3,
                             "пять": 5, "десять": 10, "пятнадцать": 15, "тридцать": 30}
                    if a == "полчаса":
                        amt = 0.5
                    elif a == "час":
                        amt = 1
                    else:
                        amt = words.get(a, int(a) if a.isdigit() else 0)
                    sec = amt * 3600 if "час" in u else amt * 60
                    remind_time = now + timedelta(seconds=sec)
                    time_phrase = f"через {a} {u}"
            if remind_time:
                text_to_save = re.sub(r'(завтра|сегодня|через\s+\d+\s+\w+)\s*(в\s+\d{1,2}[:\.]\d{2})?',
                                      '', text_to_save).strip()
                if not text_to_save:
                    text_to_save = "напоминание"
                ts = remind_time.strftime("%Y-%m-%d %H:%M")
                self._add_reminder(text_to_save, ts)
                say(f"Хорошо, напомню {time_phrase} про: {text_to_save}.")
            else:
                say("Не поняла время.")
            return True

        if any(w in c for w in ("что у меня", "мои напоминания", "список напоминаний")):
            if "завтра" in c:
                tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                active = self._list_reminders(filter_date=tomorrow)
                if active:
                    say("Завтра: " + ". ".join([f"в {r['time'].split()[1]} — {r['text']}" for r in active]))
                else:
                    say("На завтра пусто.")
            else:
                active = self._list_reminders()
                if active:
                    say("Все: " + ". ".join([f"{r['time']} — {r['text']}" for r in active]))
                else:
                    say("Напоминаний нет.")
            return True

        # УЛУЧШЕНИЯ / СПОСОБНОСТИ
        if any(w in c for w in ("что улучшить", "предложи улучшени", "как тебя улучшить")):
            self._suggest_improvements()
            return True

        if any(w in c for w in ("что ты умеешь", "список команд")):
            self._list_abilities()
            return True

        # НОЧНОЙ РЕЖИМ
        if "ночной режим" in c and ("выключи" in c or "отключи" in c):
            self._night_mode_override = False
            say("Ночной режим выключен.")
            self.night_mode_changed.emit(False)
            return True
        if "ночной режим" in c and ("включи" in c or "активируй" in c):
            self._night_mode_override = True
            say("Ночной режим включён.")
            self.night_mode_changed.emit(True)
            return True

        if any(w in c for w in ("проверь себя", "диагностик")):
            self._self_diagnose()
            return True

        if any(w in c for w in ("спи", "усни", "отключись")):
            self.is_awake = False
            say("Сплю.")
            self.sleep_mode.emit()
            return True

        if "включи диктовку" in c or "режим диктовки" in c:
            self.dictation_mode = True
            say("Диктовка включена.")
            return True

        if any(w in c for w in ("список навыков", "какие навыки")):
            if self.skills:
                say(f"Навыки: {', '.join([s['name'] for s in self.skills])}")
            else:
                say("Навыков нет.")
            return True
        if any(w in c for w in ("перезагрузи навыки", "обнови навыки")):
            say("Перезагружаю.")
            self._load_skills()
            say(f"Навыков: {len(self.skills)}")
            return True

        # ПАМЯТЬ
        if "запомни" in c:
            fact = c.replace("запомни", "").replace("что", "").strip()
            if fact:
                self.memories.append(fact)
                self._save_memory()
                self._update_system_prompt()
                say("Запомнила.")
            else:
                say("Что запомнить?")
            return True
        elif "что ты знаешь" in c or "что ты помнишь" in c:
            if self.memories:
                say("Помню: " + ". ".join(self.memories))
            else:
                say("Ничего.")
            return True
        elif "забудь все" in c:
            self.memories = []
            self._save_memory()
            self._update_system_prompt()
            say("Очистила.")
            return True

        if "научись" in c:
            request = c.replace("научись", "").strip()
            self._learn_new_skill(request)
            return True

        # ДИНАМИЧЕСКАЯ ЗАГРУЗКА custom_skills.py
        try:
            sf = APP_DIR / "custom_skills.py"
            if sf.exists():
                spec = importlib.util.spec_from_file_location("custom_skills_dynamic", sf)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "handle_custom_command") and mod.handle_custom_command(c):
                    return True
        except Exception as e:
            self.error.emit(f"custom_skills: {e}")

        if self._try_skills(c):
            return True

        # БУФЕР
        if any(w in c for w in ("буфер", "скопировал")):
            say("Читаю буфер.")
            try:
                txt = pyperclip.paste()
                if not txt or len(txt.strip()) < 5:
                    say("Пусто.")
                else:
                    txt = txt[:1500]
                    if "перевед" in c:
                        prompt = f"Переведи:\n\n{txt}"
                    elif "перескаж" in c:
                        prompt = f"Перескажи:\n\n{txt}"
                    elif "объясни" in c:
                        prompt = f"Объясни:\n\n{txt}"
                    else:
                        prompt = f"Проанализируй:\n\n{txt}"
                    self._ask_llm(prompt)
            except Exception:
                say("Ошибка.")
            return True

        # ТАЙМЕРЫ
        if any(w in c for w in ("отмени", "отключи")) and "таймер" in c:
            for t in self.active_timers:
                t.cancel()
            self.active_timers.clear()
            say("Таймеры отменены.")
            return True

        if "через" in c or "таймер" in c:
            match = re.search(
                r'(\d+|один|одну|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|пятнадцать|двадцать|тридцать)\s*(минут|минуту|минуты|час|часа|часов|секунд|секунды)',
                c)
            seconds = 0
            if "полчаса" in c:
                seconds = 1800
            elif match:
                a = match.group(1)
                u = match.group(2)
                words = {"один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3,
                         "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
                         "девять": 9, "десять": 10, "пятнадцать": 15, "двадцать": 20, "тридцать": 30}
                amt = words.get(a, int(a) if a.isdigit() else 0)
                if "час" in u:
                    seconds = amt * 3600
                elif "минут" in u:
                    seconds = amt * 60
                elif "секунд" in u:
                    seconds = amt
            if seconds > 0:
                if "выключ" in c:
                    action = "shutdown"
                    say(f"Выключу через {seconds // 60} мин.")
                elif "перезагруз" in c:
                    action = "restart"
                    say(f"Перезагружу через {seconds // 60} мин.")
                elif "блокир" in c:
                    action = "lock"
                    say(f"Заблокирую через {seconds // 60} мин.")
                else:
                    action = "notify"
                    say(f"Таймер на {seconds // 60} мин.")
                self._set_timer(seconds, action)
                return True
            else:
                say("Не поняла время.")
                return True

        # МЕДИА
        if any(w in c for w in ("пауз", "плей", "продолж")):
            if send_media_key(VK_MEDIA_PLAY_PAUSE):
                say("Готово.")
        elif any(w in c for w in ("следующ", "переключи")):
            if send_media_key(VK_MEDIA_NEXT_TRACK):
                say("Следующий.")
        elif any(w in c for w in ("предыдущ", "назад", "верни")):
            if send_media_key(VK_MEDIA_PREV_TRACK):
                say("Предыдущий.")

        # ВРЕМЯ / ДАТА
        elif any(w in c for w in ("время", "час")):
            n = datetime.now()
            say(f"{n.hour} часов {n.minute} минут.")
        elif any(w in c for w in ("дат", "число")):
            n = datetime.now()
            m = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                 "августа", "сентября", "октября", "ноября", "декабря"]
            say(f"Сегодня {n.day} {m[n.month - 1]} {n.year} года.")

        # ПОГОДА
        elif "погода" in c:
            city = "Красноярск"
            if " в " in c:
                city = c.split(" в ")[-1].strip()
            try:
                wr = requests.get(f"https://wttr.in/{city}?format=%C+%t&lang=ru", timeout=5)
                say(f"В {city}: {wr.text}")
            except Exception:
                say("Ошибка.")

        # ЗРЕНИЕ
        elif any(w in c for w in ("посмотри на экран", "скриншот")):
            say("Снимок.")
            img = self._capture_screen()
            self._ask_llm("Опиши, что на экране.", img)
        elif any(w in c for w in ("посмотри в камеру", "включи камеру")):
            say("Смотрю.")
            img = self._capture_webcam()
            if img:
                self._ask_llm("Опиши, что видишь.", img)
            else:
                say("Камера недоступна.")

        # ПРОГРАММЫ
        elif "блокнот" in c:
            if self._can_open_window("Блокнот"):
                subprocess.Popen(["notepad.exe"])
                say("Открываю.")
        elif "калькулятор" in c:
            if self._can_open_window("Калькулятор"):
                subprocess.Popen(["calc.exe"])
                say("Открываю.")
        elif "проводник" in c:
            if self._can_open_window("Проводник"):
                subprocess.Popen(["explorer.exe"])
                say("Открываю.")

        # САЙТЫ
        elif any(w in c for w in ("youtube", "ютуб", "ютюб")):
            if any(w in c for w in ("главн", "домашн")):
                if self._can_open_window("YouTube"):
                    say("Открываю YouTube.")
                    self.open_url.emit("https://www.youtube.com")
                return True
            query = c
            for w in ["включи", "найди", "на", "в", "ютубе", "ютуб", "ютюб", "youtube",
                      "видео", "песню", "пожалуйста", "ева", "первое", "клип", "музыку"]:
                query = query.replace(w, "").strip()
            query = " ".join(query.split())
            if query:
                if self._can_open_window("YouTube"):
                    say(f"Ищу: {query}")
                    self.open_url.emit(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
            else:
                if self._can_open_window("YouTube"):
                    say("Открываю YouTube.")
                    self.open_url.emit("https://www.youtube.com")
        elif "яндекс" in c and "музык" in c:
            if self._can_open_window("Музыку"):
                self.open_url.emit("https://music.yandex.ru")
                say("Открываю.")
        elif "яндекс" in c:
            if self._can_open_window("Яндекс"):
                self.open_url.emit("https://ya.ru")
                say("Открываю.")
        elif "вконтакте" in c or c == "вк" or " вк " in c:
            if self._can_open_window("ВК"):
                self.open_url.emit("https://vk.com")
                say("Открываю ВК.")

        # ЗВУК
        elif any(w in c for w in ("громче", "прибавь")):
            if keyboard:
                for _ in range(6):
                    keyboard.send('volume up')
                say("Громче.")
        elif any(w in c for w in ("тише", "убавь")):
            if keyboard:
                for _ in range(6):
                    keyboard.send('volume down')
                say("Тише.")
        elif "выключи звук" in c or "мут" in c:
            if keyboard:
                keyboard.send('volume mute')
                say("Выключила.")
        elif "включи звук" in c:
            if keyboard:
                keyboard.send('volume mute')
                say("Включила.")
        elif "заблокируй" in c:
            say("Блокирую.")
            time.sleep(2)
            ctypes.windll.user32.LockWorkStation()
        elif "выключись" in c:
            say("До встречи.")
            return False
        else:
            self._ask_llm(c)
        return True

    # ============ ЗАХВАТ ============

    def _capture_screen(self):
        ss = pyautogui.screenshot()
        b = io.BytesIO()
        ss.save(b, format="JPEG")
        return base64.b64encode(b.getvalue()).decode('utf-8')

    def _capture_webcam(self):
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return None
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return None
        _, buffer = cv2.imencode('.jpg', frame)
        return base64.b64encode(buffer).decode('utf-8')