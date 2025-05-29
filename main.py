# This project implements a voice chat bot that allows you to converse with AI while sharing your PC screen.
# You can interact with the AI using your voice while the AI can see your screen in real-time.

import asyncio
import base64
import io
import os
import sys
import traceback
from dotenv import load_dotenv

import pyaudio
import PIL.Image
import mss

from google import genai
from google.genai import types

if sys.version_info < (3, 11, 0):
    import taskgroup, exceptiongroup
    asyncio.TaskGroup = taskgroup.TaskGroup
    asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 1024

# 新しいモデル名に更新
MODEL = "gemini-2.5-flash-preview-native-audio-dialog"

load_dotenv()  # Load variables from .env

api_key = os.getenv('GEMINI_API_KEY')
system_prompt = os.getenv('SYSTEM_PROMPT', 'You are a professional and detailed AI assistant. Please provide as thorough an answer as possible to the user\'s questions.')

client = genai.Client(
    api_key=api_key,
    http_options={"api_version": "v1alpha", "timeout": 30000}  # タイムアウト30秒
)

# 新しい設定形式に更新
CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    system_instruction=system_prompt
)

pya = pyaudio.PyAudio()

class AudioLoop:
    def __init__(self):
        self.audio_in_queue = None
        self.audio_out_queue = None
        self.data_out_queue = None

        self.session = None

        self.send_text_task = None
        self.receive_audio_task = None
        self.play_audio_task = None

        self.audio_stream = None
        self.play_stream = None
        
        self.is_running = True

    async def initialize_session(self):
        """Live API用のセッションを初期化する"""
        try:
            self.session = await client.aio.live.connect(model=MODEL, config=CONFIG)
            print("Live API session established")
            return True
        except Exception as e:
            print(f"Error initializing Live API session: {e}")
            return False

    async def close_session(self):
        """Live APIセッションを閉じる"""
        if hasattr(self, 'session') and self.session:
            try:
                await self.session.close()
                print("Live API session closed")
            except Exception as e:
                print(f"Error closing Live API session: {e}")

    async def send_text(self):
        while self.is_running:
            try:
                text = await asyncio.to_thread(
                    input,
                    "message > ",
                )
                if text.lower() == "q":
                    self.is_running = False
                    break
                # 新しいAPIに合わせて送信方法を更新
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": text or "."}]}, 
                    turn_complete=True
                )
            except EOFError:
                print("\nInput stream ended. Retrying in 1 second...")
                await asyncio.sleep(1)
                continue
            except Exception as e:
                print(f"\nError in send_text: {e}")
                await asyncio.sleep(1)
                continue

    def _get_frame(self, sct):
        try:
            monitor = sct.monitors[1]  # Use the primary monitor
            sct_img = sct.grab(monitor)
            img = PIL.Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            img.thumbnail([1024, 1024])

            image_io = io.BytesIO()
            img.save(image_io, format="jpeg")
            image_io.seek(0)

            mime_type = "image/jpeg"
            image_bytes = image_io.read()
            return {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}
        except Exception as e:
            print(f"Error in _get_frame: {e}")
            return None

    async def get_frames(self):
        with mss.mss() as sct:
            while self.is_running:
                try:
                    frame_data = await asyncio.to_thread(self._get_frame, sct)
                    if frame_data is None:
                        await asyncio.sleep(1)
                        continue

                    await asyncio.sleep(2.0)  # キャプチャ間隔2秒
                    await self.data_out_queue.put(frame_data)
                except Exception as e:
                    print(f"Error in get_frames: {e}")
                    await asyncio.sleep(2)  # エラー時の待機時間2秒

    async def send_realtime(self):
        async def process_audio_queue():
            retry_count = 0
            max_retries = 3
            while self.is_running:
                try:
                    audio_msg = await self.audio_out_queue.get()
                    # 新しいAPIに合わせて音声送信方法を更新
                    await self.session.send_realtime_input(
                        audio=types.Blob(data=audio_msg["data"], mime_type="audio/pcm;rate=16000")
                    )
                    retry_count = 0  # 成功したらリトライカウントをリセット
                except Exception as e:
                    print(f"Error in process_audio_queue: {e}")
                    retry_count += 1
                    if retry_count >= max_retries:
                        print("Maximum retries exceeded in process_audio_queue, waiting longer...")
                        await asyncio.sleep(5)  # 待機時間5秒
                        retry_count = 0
                    else:
                        await asyncio.sleep(1)

        async def process_data_queue():
            retry_count = 0
            max_retries = 3
            while self.is_running:
                try:
                    data_msg = await self.data_out_queue.get()
                    # 新しいAPIに合わせて画像送信方法を更新
                    await self.session.send_client_content(
                        turns={"role": "user", "parts": [{"inline_data": data_msg}]},
                        turn_complete=False  # 画像は会話の一部として送信
                    )
                    retry_count = 0  # 成功したらリトライカウントをリセット
                except Exception as e:
                    print(f"Error in process_data_queue: {e}")
                    retry_count += 1
                    if retry_count >= max_retries:
                        print("Maximum retries exceeded in process_data_queue, waiting longer...")
                        await asyncio.sleep(5)  # 待機時間5秒
                        retry_count = 0
                    else:
                        await asyncio.sleep(1)

        await asyncio.gather(process_audio_queue(), process_data_queue())

    async def listen_audio(self):
        try:
            mic_info = pya.get_default_input_device_info()
            self.audio_stream = await asyncio.to_thread(
                pya.open,
                format=FORMAT,
                channels=CHANNELS,
                rate=SEND_SAMPLE_RATE,
                input=True,
                input_device_index=mic_info["index"],
                frames_per_buffer=CHUNK_SIZE,
            )
            if __debug__:
                kwargs = {"exception_on_overflow": False}
            else:
                kwargs = {}
            while self.is_running:
                try:
                    data = await asyncio.to_thread(self.audio_stream.read, CHUNK_SIZE, **kwargs)
                    await self.audio_out_queue.put({"data": data, "mime_type": "audio/pcm"})
                except Exception as e:
                    print(f"Error in listen_audio: {e}")
                    await asyncio.sleep(1)
        except Exception as e:
            print(f"Error initializing audio stream: {e}")

    # 新しいレスポンス受信メソッド
    async def receive_responses(self):
        """Live APIからのレスポンスを受信して処理する"""
        retry_count = 0
        max_retries = 3
        while self.is_running:
            try:
                async for response in self.session.receive():
                    if response.audio is not None:
                        # 音声レスポンスの処理
                        audio_data = response.audio.data
                        await self.audio_in_queue.put(audio_data)
                    elif response.text is not None:
                        # テキストレスポンスの処理（デバッグ用）
                        print(response.text, end="")
                        # Check if the user said goodbye (case-insensitive, handles variations)
                        if response.text and ("goodbye" in response.text.strip().lower() or "good bye" in response.text.strip().lower()):
                            print("\nUser said goodbye. Exiting...")
                            self.is_running = False
                            break # Exit the async for loop
                
                retry_count = 0  # 成功したらリトライカウントをリセット
            except Exception as e:
                print(f"Error in receive_responses: {e}")
                retry_count += 1
                if retry_count >= max_retries:
                    print("Maximum retries exceeded in receive_responses, waiting longer...")
                    await asyncio.sleep(5)  # より長い待機時間
                    retry_count = 0
                else:
                    await asyncio.sleep(1)

    async def play_audio(self):
        try:
            self.play_stream = await asyncio.to_thread(
                pya.open,
                format=FORMAT,
                channels=CHANNELS,
                rate=RECEIVE_SAMPLE_RATE,
                output=True,
            )
            while self.is_running:
                try:
                    bytestream = await self.audio_in_queue.get()
                    await asyncio.to_thread(self.play_stream.write, bytestream)
                except Exception as e:
                    print(f"Error in play_audio: {e}")
                    sys.exit(0)
        except Exception as e:
            print(f"Error initializing play stream: {e}")

    async def run(self):
        try:
            # セッション初期化
            session_initialized = await self.initialize_session()
            if not session_initialized:
                print("Failed to initialize Live API session. Exiting.")
                return

            self.audio_in_queue = asyncio.Queue()
            self.audio_out_queue = asyncio.Queue(maxsize=5)
            self.data_out_queue = asyncio.Queue(maxsize=5)

            # タスクグループの更新
            async with asyncio.TaskGroup() as tg:
                send_text_task = tg.create_task(self.send_text())
                tg.create_task(self.send_realtime())
                tg.create_task(self.listen_audio())
                tg.create_task(self.get_frames())
                tg.create_task(self.receive_responses())  # 新しいレスポンス受信タスク
                tg.create_task(self.play_audio())

                await send_text_task
                self.is_running = False

        except asyncio.CancelledError:
            self.is_running = False
        except ExceptionGroup as EG:
            traceback.print_exception(EG)
        finally:
            self.is_running = False
            # セッションのクリーンアップ
            await self.close_session()
            # Stop and close the audio stream if it exists
            if self.audio_stream:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
            # Stop and close the play stream if it exists
            if self.play_stream:
                self.play_stream.stop_stream()
                self.play_stream.close()
            # Terminate PyAudio object
            pya.terminate()

if __name__ == "__main__":
    main = AudioLoop()
    asyncio.run(main.run())
