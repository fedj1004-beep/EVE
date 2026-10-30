import json, queue, subprocess, sys, threading, time, os, io, base64
import requests
import pygame
from gtts import gTTS
from datetime import datetime
from pathlib import Path
import re

class Brain:
    def __init__(self, api_key, app_dir, memory_file, speak_callback, error_callback):
        self.api_key = api_key
        self.app_dir = app_dir
        self.memory_file = memory_file
        self.speak_callback = speak_callback
        self.error_callback = error_callback
        
        self.memories = self._load_memory()
        self.chat_history = []
        self._update_system_prompt()
        
        # Очередь для стриминга озвучки
        self.tts_queue = queue.Queue()
        self._tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self._tts_thread.start()
        
        self.is_speaking = False

    def _load_memory(self):
        if self.memory_file.exists():
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except: return []
        return []

    def save_memory(self):
        try:
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(self.memories, f, ensure_ascii=False, indent=4)
        except Exception as e:
            self.error_callback(f"Ошибка памяти: {e}")

    def _update_system_prompt(self):
        base = ("Ты — голосовой ассистент Ева. Отвечай кратко, дружелюбно, на русском языке. "
                "Никогда не используй маркдаун, смайлики и списки. Пиши только чистый текст. ")
        now = datetime.now()
        base += f"ВАЖНО: Сегодня {now.strftime('%d.%m.%Y, %H:%M')}. "
        if self.memories:
            base += "Ты знаешь о пользователе: " + "; ".join(self.memories) + ". "
        self.system_prompt = base

    # ============ ОЗВУЧКА (ПОТОКОВАЯ) ============
    def _tts_worker(self):
        """Фоновый поток: берет предложения из очереди, генерирует аудио и играет"""
        while True:
            text = self.tts_queue.get()
            if text is None:
                break
            
            if not text.strip():
                self.tts_queue.task_done()
                continue
                
            self.is_speaking = True
            self.speak_callback(text) # Отправляем текст в GUI
            
            temp_file = self.app_dir / f"eve_temp_{int(time.time()*1000)}.mp3"
            try:
                tts = gTTS(text=text, lang='ru')
                tts.save(str(temp_file))
                pygame.mixer.music.load(str(temp_file))
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.Clock().tick(10)
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
                try: os.remove(temp_file)
                except: pass
            except Exception as e:
                self.error_callback(f"Ошибка gTTS: {e}")
            
            self.tts_queue.task_done()
            self.is_speaking = False

    def stop_speaking(self):
        """Останавливает текущую озвучку и очищает очередь"""
        try:
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
        except: pass
        while not self.tts_queue.empty():
            try:
                self.tts_queue.get_nowait()
                self.tts_queue.task_done()
            except: pass
        self.is_speaking = False

    # ============ СТРИМИНГ LLM ============
    def ask_llm_stream(self, text, image_base64=None):
        self._update_system_prompt()
        
        messages = [{"role": "system", "content": self.system_prompt}]
        history_without_system = self.chat_history[1:] if len(self.chat_history) > 1 else []
        
        content = [{"type": "text", "text": text}]
        if image_base64:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}})
        
        messages.extend(history_without_system)
        messages.append({"role": "user", "content": content if image_base64 else text})
        
        self.chat_history.append({"role": "user", "content": text})
        
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        data = {
            "model": "gpt-4o", 
            "messages": messages, 
            "max_tokens": 400, 
            "temperature": 0.7,
            "stream": True # ВКЛЮЧАЕМ СТРИМИНГ
        }
        
        full_answer = ""
        buffer = ""
        
        try:
            response = requests.post(
                "https://api.proxyapi.ru/openai/v1/chat/completions",
                headers=headers, json=data, stream=True, timeout=60
            )
            
            if response.status_code != 200:
                self.error_callback(f"API {response.status_code}")
                return

            for line in response.iter_lines():
                if not line:
                    continue
                
                line_text = line.decode('utf-8')
                if line_text.startswith("data: "):
                    line_text = line_text[6:]
                if line_text == "[DONE]":
                    break
                    
                try:
                    chunk = json.loads(line_text)
                    delta = chunk["choices"][0]["delta"]
                    if "content" in delta:
                        word = delta["content"]
                        full_answer += word
                        buffer += word
                        
                        # Разбиваем на предложения (по точкам, вопросам, восклицаниям)
                        # Если в буфере есть знак препинания и пробел после него
                        if re.search(r'[.!?]\s', buffer) or len(buffer) > 200:
                            sentences = re.split(r'(?<=[.!?])\s+', buffer)
                            for s in sentences[:-1]:
                                if s.strip():
                                    self.tts_queue.put(s.strip() + ".")
                            buffer = sentences[-1]
                            
                except Exception:
                    pass
            
            # Добавляем остаток
            if buffer.strip():
                self.tts_queue.put(buffer.strip())
            
            self.chat_history.append({"role": "assistant", "content": full_answer})
            if len(self.chat_history) > 11:
                self.chat_history = [self.chat_history[0]] + self.chat_history[-10:]
                
        except Exception as e:
            self.error_callback(f"Ошибка стриминга: {e}")

    def ask_llm_blocking(self, prompt):
        """Обычный запрос без стриминга (для самоанализа и обучения)"""
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        data = {"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
        try:
            r = requests.post("https://api.proxyapi.ru/openai/v1/chat/completions", headers=headers, json=data, timeout=60)
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            self.error_callback(f"Ошибка API: {e}")
            return None