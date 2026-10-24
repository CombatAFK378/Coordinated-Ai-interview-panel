"""Inbound phone line: Ask the Panel by calling the panel's number.

A LiveKit dispatch rule (phone_setup.py) puts every call to the panel's number
in a fresh `call-…` room and dispatches agent `settings.phone_agent_name`. This
registers the worker under that name, in-process — jobs run as threads, so no
extra worker process — and LiveKit hands us each call as it rings.

The job itself never joins the room. It hands the room to the app, which joins
with the normal panel voice session (the same one browser Ask the Panel uses),
and waits until that call ends; then it deletes the room, hanging up the phone.
"""
import asyncio
import logging
import math
import threading

from livekit.agents import AgentServer, JobContext, JobExecutorType

logger = logging.getLogger(__name__)


class PhoneLine:
    def __init__(self, settings, on_call):
        """`on_call(room)` takes the call and returns a threading.Event that is set
        when the call ends, or None to refuse it (the caller is hung up)."""
        self.settings = settings
        self.on_call = on_call
        self._loop = None
        self._server = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="phone-line")

    def start(self):
        self._thread.start()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        s = self.settings
        self._server = AgentServer(
            ws_url=s.livekit_url, api_key=s.livekit_api_key, api_secret=s.livekit_api_secret,
            job_executor_type=JobExecutorType.THREAD,
            # No prewarmed jobs; the health server takes any free port (8080 is ours).
            num_idle_processes=0, port=0,
            # The app takes one call at a time itself; don't let a CPU spike from
            # the voice pipeline mark the line busy.
            load_threshold=math.inf,
        )
        self._server.rtc_session(self._take_call, agent_name=s.phone_agent_name)
        logger.info("phone line: registering as agent %r", s.phone_agent_name)
        try:
            self._loop.run_until_complete(self._server.run())
        except Exception:
            logger.exception("phone line stopped")
        finally:
            self._loop.close()

    async def _take_call(self, ctx: JobContext):
        room = ctx.job.room.name
        logger.info("phone: incoming call in %s", room)
        done = await asyncio.to_thread(self.on_call, room)
        if done is None:
            logger.warning("phone: call in %s refused", room)
        else:
            # The panel session is in the room, not this job, so the SDK's "room
            # connection was not established" warning after 10s is expected.
            while not done.is_set():
                await asyncio.sleep(0.5)
        try:
            await ctx.delete_room()   # hangs up the caller if they're still on
        except Exception as e:
            logger.warning("phone: could not close %s (%s)", room, e)
        logger.info("phone: call in %s ended", room)
        ctx.shutdown(reason="call ended")

    def stop(self):
        if self._server is not None and self._loop is not None and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._server.aclose(), self._loop)
            try:
                fut.result(timeout=10)
            except Exception:
                logger.warning("phone line shutdown did not finish cleanly")
        self._thread.join(timeout=5)
