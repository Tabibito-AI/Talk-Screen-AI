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

if sys.version_info < (3, 11, 0):
    import taskgroup, exceptiongroup
    asyncio.TaskGroup = taskgroup.TaskGroup
    asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 1024

MODEL = "models/gemini-2.0-flash-exp" # gemini-2.0-flash-exp gemini-2.0-flash-thinking-exp-1219

load_dotenv()  # Load variables from .env

api_key = os.getenv('GEMINI_API_KEY')

client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})

CONFIG = {"generation_config": {"response_modalities": ["AUDIO"]}}

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

    async def send_text(self):
        while True:
            text = await asyncio.to_thread(
                input,
                "message > ",
            )
            if text.lower() == "q":
                break
            await self.session.send(text or ".", end_of_turn=True)

    def _get_frame(self, sct):
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

    async def get_frames(self):
        with mss.mss() as sct:
            while True:
                frame_data = await asyncio.to_thread(self._get_frame, sct)
                if frame_data is None:
                    break

                await asyncio.sleep(1.0)

                await self.data_out_queue.put(frame_data)

    async def send_realtime(self):
        async def process_audio_queue():
            while True:
                audio_msg = await self.audio_out_queue.get()
                await self.session.send(input=audio_msg)

        async def process_data_queue():
            while True:
                data_msg = await self.data_out_queue.get()
                await self.session.send(input=data_msg)

        await asyncio.gather(process_audio_queue(), process_data_queue())

    async def listen_audio(self):
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
        while True:
            data = await asyncio.to_thread(self.audio_stream.read, CHUNK_SIZE, **kwargs)
            await self.audio_out_queue.put({"data": data, "mime_type": "audio/pcm"})

    async def receive_audio(self):
        "Background task to reads from the websocket and write pcm chunks to the output queue"
        while True:
            turn = self.session.receive()
            async for response in turn:
                if data := response.data:
                    self.audio_in_queue.put_nowait(data)
                    continue
                if text := response.text:
                    print(text, end="")

            while not self.audio_in_queue.empty():
                self.audio_in_queue.get_nowait()

    async def play_audio(self):
        self.play_stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=RECEIVE_SAMPLE_RATE,
            output=True,
        )
        while True:
            bytestream = await self.audio_in_queue.get()
            await asyncio.to_thread(self.play_stream.write, bytestream)

    async def run(self):
        try:
            async with (
                client.aio.live.connect(model=MODEL, config=CONFIG) as session,
                asyncio.TaskGroup() as tg,
            ):
                self.session = session

                self.audio_in_queue = asyncio.Queue()
                self.audio_out_queue = asyncio.Queue(maxsize=5)
                self.data_out_queue = asyncio.Queue(maxsize=5)

                send_text_task = tg.create_task(self.send_text())
                tg.create_task(self.send_realtime())
                tg.create_task(self.listen_audio())
                tg.create_task(self.get_frames())
                tg.create_task(self.receive_audio())
                tg.create_task(self.play_audio())

                await send_text_task
                raise asyncio.CancelledError("User requested exit")

        except asyncio.CancelledError:
            pass
        except ExceptionGroup as EG:
            traceback.print_exception(EG)
        finally:
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