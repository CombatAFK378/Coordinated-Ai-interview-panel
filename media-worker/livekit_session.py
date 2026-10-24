"""LiveKit voice I/O, embedded in the single-session FastAPI worker.

One RTC participant carries the panel's serialized floor. Each persona uses its
own Deepgram TTS plugin; the application chooses both the brain and the voice.
LiveKit owns streaming STT, BVC, v1-mini endpointing, playout and interruptions.
There is no second managed LLM or cloud callback endpoint.
"""
import asyncio
import concurrent.futures
import logging
import threading
from dataclasses import dataclass

import aiohttp
from livekit import rtc
from livekit.agents import Agent, AgentSession, inference, room_io
from livekit.plugins import deepgram, noise_cancellation, silero

from shared import prompts
from shared.livekit_token import build_room_token

logger = logging.getLogger(__name__)


@dataclass
class SpokenTurn:
    text: str
    interrupted: bool


class PanelVoiceAgent(Agent):
    def __init__(self, transport):
        super().__init__(instructions="The application supplies all grounded panel responses.")
        self.transport = transport

    async def on_user_turn_completed(self, turn_ctx, new_message):
        # With no SDK LLM configured this hook consumes the completed turn and
        # returns without a second automatic response. Domain work runs off-loop.
        text = new_message.text_content.strip()
        self.transport._partial = ""
        if text and self.transport.on_text:
            self.transport.on_text(text)


class LiveKitSession:
    def __init__(self, settings, room: str, identity: str, *, recruiter=False,
                 phone=False, on_event=None):
        self.settings = settings
        self.room_name = room
        self.identity = identity
        # A phone (SIP) caller: its identity isn't known up front, so the session
        # links to whichever SIP participant is in the room, and its audio is
        # narrowband telephony.
        self.phone = phone
        # Barge-in policy differs by surface:
        #  - Interview: follows settings.allow_bargein. On speakers, echo/noise
        #    falsely triggers barge-in and chops interviewers off mid-word (Liam's
        #    "share your screen" got cut), so the demo default is OFF — agents
        #    finish their questions.
        #  - Ask-the-Panel (recruiter): ALWAYS on. The recruiter is driving and
        #    expects to cut an interviewer off and redirect ("ok, but Liam—").
        self.allow_interruptions = recruiter or settings.allow_bargein
        self.on_event = on_event or (lambda event: None)
        self.on_text = None
        self._on_joined = None
        self._on_left = None
        self._joined = False
        self._partial = ""
        self._loop = None
        self._room = None
        self._voice = None
        self._http = None
        self._stt = None
        self._voices = {}
        self._handle = None
        self._task = None
        self._shutdown = None
        self._stopping = False
        self._speaking = threading.Event()
        self._ready = concurrent.futures.Future()
        self._thread = threading.Thread(target=self._run, daemon=True, name="livekit-io")

    def set_on_user_joined(self, callback):
        self._on_joined = callback

    def set_on_user_left(self, callback):
        """Called (on the voice loop thread) when the joined user leaves — for a
        phone caller, when they hang up."""
        self._on_left = callback

    def _is_user(self, participant) -> bool:
        if self.phone:
            return participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        return participant.identity == self.identity

    def start(self):
        self._thread.start()
        try:
            self._ready.result(timeout=45)
        except BaseException:
            self.stop()
            raise

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._shutdown = asyncio.Event()

        async def serve():
            try:
                await asyncio.wait_for(self._connect(), timeout=40)
                self._ready.set_result(None)
                await self._shutdown.wait()
            except BaseException as exc:
                if not self._ready.done():
                    self._ready.set_exception(exc)
                if not isinstance(exc, asyncio.CancelledError):
                    logger.exception("LiveKit session failed")
            finally:
                self._stopping = True
                await self._close()

        try:
            self._task = self._loop.create_task(serve())
            self._loop.run_until_complete(self._task)
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _connect(self):
        self._http = aiohttp.ClientSession()
        self._room = rtc.Room()
        self._stt = deepgram.STT(
            api_key=self.settings.deepgram_api_key,
            model=self.settings.livekit_stt_model,
            language=self.settings.livekit_stt_language,
            http_session=self._http,
        )
        self._voices = {
            aid: deepgram.TTS(api_key=self.settings.deepgram_api_key,
                              model=agent.voice_model, http_session=self._http)
            for aid, agent in prompts.AGENTS.items()
        }
        self._voice = AgentSession(
            stt=self._stt, vad=silero.VAD.load(),
            # Set explicitly: Docker uses the local audio model in every mode.
            turn_handling={
                "turn_detection": inference.TurnDetector(version="v1-mini"),
                "endpointing": {
                    "min_delay": self.settings.livekit_endpointing_min_delay,
                    "max_delay": self.settings.livekit_endpointing_max_delay,
                },
                "interruption": {"enabled": self.allow_interruptions,
                                 "resume_false_interruption": True},
                "preemptive_generation": {"enabled": False},
            },
        )

        @self._voice.on("user_input_transcribed")
        def transcribed(event):
            preview = (self._partial + " " + event.transcript).strip()
            if event.is_final:
                self._partial = preview
            self.on_event({"type": "partial", "text": preview})

        @self._voice.on("error")
        def voice_error(event):
            logger.error("LiveKit voice error: %s", event.error)
            self.on_event({"type": "voice_error", "message": "Voice service error. Please retry."})

        @self._room.on("track_subscribed")
        def subscribed(track, publication, participant):
            if (self._is_user(participant) and track.kind == rtc.TrackKind.KIND_AUDIO
                    and not self._joined):
                self._joined = True
                if self._on_joined:
                    self._on_joined(participant.identity)

        @self._room.on("participant_disconnected")
        def left(participant):
            if self._joined and self._is_user(participant) and self._on_left:
                self._on_left(participant.identity)

        token = build_room_token(self.settings, self.room_name, "panel", agent=True)
        await self._room.connect(self.settings.livekit_url, token)
        filters = {"bvc": noise_cancellation.BVC, "nc": noise_cancellation.NC}
        if self.phone:   # BVC's telephony model is tuned for narrowband call audio
            filters = {"bvc": noise_cancellation.BVCTelephony,
                       "nc": noise_cancellation.BVCTelephony}
        make_filter = filters.get(self.settings.livekit_noise_cancellation)
        link = ({"participant_kinds": [rtc.ParticipantKind.PARTICIPANT_KIND_SIP]}
                if self.phone else {"participant_identity": self.identity})
        await self._voice.start(
            agent=PanelVoiceAgent(self), room=self._room,
            room_options=room_io.RoomOptions(
                **link,
                audio_input=room_io.AudioInputOptions(
                    noise_cancellation=make_filter() if make_filter else None,
                ),
                text_input=False, video_input=False,
                # The HTTP session lifecycle owns shutdown, including reconnects.
                close_on_disconnect=False,
            ),
        )

    def _call(self, coro, timeout=30):
        if self._stopping or not self._loop or not self._loop.is_running():
            coro.close()
            raise RuntimeError("LiveKit session is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    async def _speak(self, agent_id, text):
        # Streaming plugin audio is supplied per utterance, so switching voices
        # cannot leave an old Agent handoff or a second listener running.
        frames = {"n": 0}

        async def audio():
            async with self._voices[agent_id].stream() as stream:
                stream.push_text(text)
                stream.end_input()
                async for event in stream:
                    frames["n"] += 1
                    yield event.frame
            logger.info("TTS[%s] produced %d frames", agent_id, frames["n"])

        self._speaking.set()
        try:
            logger.info("speak start agent=%s chars=%d", agent_id, len(text))
            self._handle = self._voice.say(
                text, audio=audio(), allow_interruptions=self.allow_interruptions,
            )
            await self._handle
            logger.info("speak done agent=%s interrupted=%s frames=%d",
                        agent_id, self._handle.interrupted, frames["n"])
            error = self._handle.exception()
            if error:
                raise error
            heard = " ".join(item.text_content for item in self._handle.chat_items
                             if getattr(item, "role", None) == "assistant")
            return SpokenTurn(heard, self._handle.interrupted)
        finally:
            if self._handle and not self._handle.done():
                self._handle.interrupt(force=True)
            self._handle = None
            self._speaking.clear()

    def speak(self, agent_id, text):
        return self._call(self._speak(agent_id, text), timeout=180)

    def is_speaking(self):
        return self._speaking.is_set()

    def interrupt(self):
        async def interrupt():
            if self._voice:
                await self._voice.interrupt(force=True)
        return self._call(interrupt())

    def finish_turn(self):
        async def commit():
            await self._voice.commit_user_turn()
            return True
        return self._call(commit())

    def clear_user_turn(self):
        async def clear():
            self._partial = ""
            self._voice.clear_user_turn()
        self._call(clear())

    async def _close(self):
        if self._voice:
            try:
                if self._handle and not self._handle.done():
                    self._handle.interrupt(force=True)
                await self._voice.aclose()
            except Exception:
                logger.exception("voice shutdown failed")
        results = await asyncio.gather(
            *(p.aclose() for p in [self._stt, *self._voices.values()] if p),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("speech plugin shutdown: %s", result)
        if self._room:
            try:
                await self._room.disconnect()
            except Exception:
                logger.exception("room shutdown failed")
        if self._http:
            await self._http.close()

    def stop(self):
        self._stopping = True
        if self._loop and self._loop.is_running():
            def shutdown():
                if not self._ready.done() and self._task:
                    self._task.cancel()
                elif self._shutdown:
                    self._shutdown.set()
            self._loop.call_soon_threadsafe(shutdown)
        if self._thread.is_alive():
            self._thread.join(timeout=20)
