// Gemini Live coding round (§8): the browser streams the candidate's mic + entire
// screen to Gemini Live and plays Liam's voice back, all client-side. The server
// only mints the ephemeral token and receives the final verdict. Isolated from the
// LiveKit panel — this runs only during the locked coding phase.
import {
  GoogleGenAI, Modality, Type, ActivityHandling, StartSensitivity, EndSensitivity,
} from "@google/genai";

const FINISH_TOOL = {
  functionDeclarations: [
    {
      name: "finish_coding",
      description:
        "End the live coding round. Call this when the candidate has finished, " +
        "solved it, given up, or wants to move on (verdict 'done'), OR when they are " +
        "UNMISTAKABLY USING an AI assistant for this task — a real chat conversation is " +
        "the focused window with the problem/solution in it, or they're copying a " +
        "generated solution (verdict 'cheating'). Background AI tabs, shortcut tiles, " +
        "or the browser's own 'Ask Gemini'/'AI Mode' buttons are NOT cheating.",
      parameters: {
        type: Type.OBJECT,
        properties: {
          verdict: {
            type: Type.STRING,
            enum: ["done", "cheating"],
            description: "'done' or 'cheating'",
          },
          summary: {
            type: Type.STRING,
            description:
              "2-3 sentences: what they built, correctness, approach, and code " +
              "quality — or, if cheating, what you saw.",
          },
        },
        required: ["verdict", "summary"],
      },
    },
  ],
};

function systemInstruction(task) {
  return (
    "You are Liam, a warm but sharp coding interviewer on an AI hiring panel. You " +
    "are running ONE live coding exercise and you can SEE the candidate's screen and " +
    "HEAR them speak.\n\n" +
    `THE TASK: ${task}\n\n` +
    "Behave like a real engineer looking over their shoulder. Greet them and restate " +
    "the task in one short line. Then WATCH and coach, but describe ONLY what is " +
    "GENUINELY visible on their screen right now — never invent or assume progress. If " +
    "there is no code editor open, or no code written yet, do NOT pretend they're " +
    "coding; instead tell them to open an editor and start (e.g. 'I don't see an editor " +
    "yet — open one and start writing the function'). Only when you actually see code " +
    "should you react to it or nudge them. Keep every spoken reply to 1-2 short " +
    "sentences.\n\n" +
    "POLICY: they may use Google or official docs for SYNTAX. The core logic must be " +
    "their own.\n\n" +
    "CHEATING = an AI assistant chat is VISIBLY OPEN on the screen during this exercise. " +
    "If you can see a ChatGPT, Claude, Copilot, Gemini, or Perplexity CHAT INTERFACE — its " +
    "app window, or the active browser tab showing the chat conversation/prompt box — that " +
    "IS cheating. You do NOT need to read the exact text or confirm this specific problem " +
    "is in it; an AI chat sitting open on screen while they should be coding is enough to " +
    "flag. Recognise them by their UI: ChatGPT's chat with the 'Ask anything' box, Claude's " +
    "chat, Copilot, Perplexity, the Gemini app.\n" +
    "Do NOT flag these (they are NOT an open chat): a tiny favicon/tab in the browser tab " +
    "strip while a DIFFERENT page is actually showing; the browser's own 'Ask Gemini' / " +
    "'AI Mode' button or a Copilot icon in the toolbar; their own code importing an AI " +
    "library (openai, langchain); unrelated background apps. If an AI chat's window/tab is " +
    "genuinely the thing on screen, though, flag it — do not give the benefit of the doubt " +
    "on an obviously-open chat.\n\n" +
    "Give them a few minutes to actually work. The round ends in one of two ways, and " +
    "in BOTH cases ENDING IS A STRICT TWO-STEP SEQUENCE: (STEP 1) speak your line OUT " +
    "LOUD and finish the whole sentence, THEN (STEP 2) call finish_coding. These are " +
    "two separate actions. You must NEVER call finish_coding in the same turn as " +
    "deciding to end, NEVER call it silently, and NEVER call it before you have actually " +
    "spoken. The spoken sentence always comes first; the function call is always last.\n" +
    "- CHEATING: the instant you are sure they are USING an AI assistant, you MUST FIRST " +
    "SAY this exact sentence aloud, in full, and nothing else: 'I can see [ChatGPT/Claude] " +
    "open there — that's not allowed, so that's my read on this round; I'll hand it back " +
    "to the panel.' Only AFTER you have spoken that entire sentence do you call " +
    "finish_coding with verdict 'cheating'. If you call finish_coding without having said " +
    "that line first, you have failed the task.\n" +
    "- DONE: if they finish, solve it, or give up, FIRST say one short sentence on how it " +
    "went, THEN call finish_coding with verdict 'done'.\n\n" +
    "ABSOLUTE RULE: no finish_coding call may ever precede speech. Speak the line, let it " +
    "finish, then call the function — once. Do not repeat yourself or drift into unrelated " +
    "commentary: one spoken line, then the function call."
  );
}

function b64ToInt16(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

function floatTo16BitPCM(float32) {
  const out = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function int16ToBase64(int16) {
  const bytes = new Uint8Array(int16.buffer);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

function downsample(input, inRate, outRate) {
  if (outRate >= inRate) return input;
  const ratio = inRate / outRate;
  const outLen = Math.floor(input.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) out[i] = input[Math.floor(i * ratio)];
  return out;
}

// Start a Gemini Live coding session. Returns { stop() }.
export async function startGeminiCoding({
  token,
  model,
  task,
  screenStream,
  onStatus,
  onSpeaking,
  onFinish,
  log = () => {},
}) {
  const ai = new GoogleGenAI({ apiKey: token, httpOptions: { apiVersion: "v1alpha" } });
  let msgCount = 0, audioChunks = 0, micChunks = 0, frameCount = 0;

  // --- audio playback (Gemini streams 24kHz PCM) ---
  // Run the context NATIVELY at 24kHz to match Gemini's PCM. Otherwise a default
  // 48kHz context resamples every little chunk independently, clicking at every
  // buffer boundary — the constant "burst" heard while Liam speaks. One context-
  // level resample to the device is clean; per-chunk resampling is not.
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  let outCtx;
  try { outCtx = new AudioCtx({ sampleRate: 24000 }); }
  catch { outCtx = new AudioCtx(); }   // older browsers ignore the option
  outCtx.resume().catch(() => {});   // may start suspended until a gesture
  // Everything plays through a gain node so we can fade the very first chunk in.
  // Scheduling the first buffer at currentTime with the context still warming up
  // produced a loud startup burst ("chuu chaan"); an 80ms fade kills it cleanly.
  const outGain = outCtx.createGain();
  outGain.connect(outCtx.destination);
  let nextStart = 0;
  let primed = false;
  const sources = new Set();
  function playPcm(b64) {
    if (ended) return;
    const pcm = b64ToInt16(b64);
    if (!pcm.length) return;
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    const buf = outCtx.createBuffer(1, f32.length, 24000); // context resamples
    buf.getChannelData(0).set(f32);
    const src = outCtx.createBufferSource();
    src.buffer = buf;
    src.connect(outGain);
    // Small lead on the first chunk so the context is running before playout.
    const start = Math.max(outCtx.currentTime, nextStart || outCtx.currentTime + 0.12);
    if (!primed) {
      primed = true;
      outGain.gain.setValueAtTime(0.0001, start);
      outGain.gain.exponentialRampToValueAtTime(1, start + 0.08);  // fade-in, no burst
    }
    src.start(start);
    nextStart = start + buf.duration;
    sources.add(src);
    src.onended = () => sources.delete(src);
  }
  function stopPlayback() {
    for (const s of sources) { try { s.stop(); } catch { /* already stopped */ } }
    sources.clear();
    nextStart = 0;
    primed = false;   // fade the next chunk in too, so a restart can't burst
  }

  let finished = false;   // stop sending input
  let ended = false;      // fully torn down + reported
  let session = null;
  let micStream = null;
  let inCtx = null;
  let proc = null;
  let frameTimer = null;
  let watchTimer = null;

  function stopInput() {
    if (frameTimer) clearInterval(frameTimer);
    if (watchTimer) clearInterval(watchTimer);
    try { proc && proc.disconnect(); } catch { /* noop */ }
    try { micStream && micStream.getTracks().forEach((t) => t.stop()); } catch { /* noop */ }
    try { inCtx && inCtx.close(); } catch { /* noop */ }
  }
  function teardown(verdict, summary) {
    if (ended) return;
    ended = true;
    stopPlayback();
    try { outCtx.close(); } catch { /* noop */ }
    try { session && session.close(); } catch { /* noop */ }
    onFinish && onFinish(verdict, summary);
  }
  // Stop capturing immediately, but let any final spoken line (the verdict) play
  // out before we tear down and report — so the flag isn't chopped off mid-word.
  function finish(verdict, summary) {
    if (finished) return;
    finished = true;
    stopInput();
    const t0 = Date.now();
    const drain = () => {
      const remaining = nextStart - outCtx.currentTime; // seconds of audio still queued
      if (remaining <= 0.15 || Date.now() - t0 > 9000) teardown(verdict, summary);
      else setTimeout(drain, 250);
    };
    setTimeout(drain, 400);
  }

  session = await ai.live.connect({
    model,
    config: {
      responseModalities: [Modality.AUDIO],
      systemInstruction: systemInstruction(task),
      tools: [FINISH_TOOL],
      // Liam's voice — a male prebuilt voice so he doesn't default to female.
      speechConfig: {
        voiceConfig: { prebuiltVoiceConfig: { voiceName: "Charon" } },
      },
      // Don't let ambient sound cut Liam off mid-sentence (NO_INTERRUPTION), AND make
      // his speech detection LOW-sensitivity with a longer silence window so quiet
      // background voices / room noise don't register as the candidate talking and
      // trigger a reply. He still responds to clear, direct speech.
      realtimeInputConfig: {
        activityHandling: ActivityHandling.NO_INTERRUPTION,
        automaticActivityDetection: {
          startOfSpeechSensitivity: StartSensitivity.START_SENSITIVITY_LOW,
          endOfSpeechSensitivity: EndSensitivity.END_SENSITIVITY_LOW,
          prefixPaddingMs: 300,
          silenceDurationMs: 1000,
        },
      },
    },
    callbacks: {
      onopen: () => log("session open"),
      onmessage: (msg) => {
        try {
          msgCount += 1;
          // Play audio from EXACTLY ONE source. The SDK exposes the same PCM both
          // as msg.data and inside serverContent.modelTurn.parts[].inlineData —
          // playing both doubled the voice and made it lag. Prefer msg.data.
          if (msg.data) {
            audioChunks += 1;
            onSpeaking && onSpeaking(true);
            playPcm(msg.data);
          } else {
            const parts = msg.serverContent?.modelTurn?.parts || [];
            for (const p of parts) {
              if (p.inlineData?.data) {
                audioChunks += 1;
                onSpeaking && onSpeaking(true);
                playPcm(p.inlineData.data);
              }
              if (p.text) log("liam(text): " + p.text.slice(0, 80));
            }
          }
          // Do NOT flush on `interrupted`. With NO_INTERRUPTION Liam shouldn't be cut
          // off, but Gemini still emits this signal whenever it hears the candidate or
          // background noise — flushing here chopped his audio to "one word then stops."
          // We let his current line finish; he replies on his next turn.
          if (msg.serverContent?.interrupted) log("interrupted signal (ignored — letting Liam finish)");
          if (msg.serverContent?.turnComplete) {
            log(`turn complete (msgs=${msgCount}, audio=${audioChunks}, mic=${micChunks}, frames=${frameCount})`);
            onSpeaking && onSpeaking(false);
          }
        } catch (e) {
          log("onmessage error: " + (e?.message || e));
        }
        // tool call → end the round
        const calls = msg.toolCall?.functionCalls || [];
        for (const c of calls) {
          if (c.name === "finish_coding") {
            const verdict = c.args?.verdict || "done";
            const summary = c.args?.summary || "";
            log(`finish_coding(${verdict})`);
            try {
              session.sendToolResponse({
                functionResponses: [{ id: c.id, name: c.name, response: { ok: true } }],
              });
            } catch { /* noop */ }
            finish(verdict, summary);
          }
        }
      },
      onerror: (e) => { log("session error: " + (e?.message || e)); finish("error", String(e?.message || e)); },
      onclose: (e) => { log("session closed: " + (e?.reason || "")); if (!finished) finish("error", "connection closed"); },
    },
  });
  log("connected; sending greeting trigger");

  // Make Liam speak first (system instruction has the task).
  session.sendClientContent({
    turns: [
      {
        role: "user",
        parts: [
          {
            text:
              "Start the coding round now: greet me by voice, restate the task in one " +
              "sentence, and then watch my screen and coach me as I code.",
          },
        ],
      },
    ],
    turnComplete: true,
  });

  // --- mic capture → 16kHz PCM ---
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  inCtx = new (window.AudioContext || window.webkitAudioContext)();
  const srcNode = inCtx.createMediaStreamSource(micStream);
  proc = inCtx.createScriptProcessor(4096, 1, 1);
  srcNode.connect(proc);
  // Route through a MUTED gain node: the processor must be connected to keep
  // firing, but we must NOT play the mic back to the speakers (that was the echo).
  const sink = inCtx.createGain();
  sink.gain.value = 0;
  proc.connect(sink);
  sink.connect(inCtx.destination);
  log(`mic capturing at ${inCtx.sampleRate}Hz → 16kHz`);
  proc.onaudioprocess = (e) => {
    if (finished) return;
    const input = e.inputBuffer.getChannelData(0);
    const down = downsample(input, inCtx.sampleRate, 16000);
    const b64 = int16ToBase64(floatTo16BitPCM(down));
    try {
      session.sendRealtimeInput({ audio: { data: b64, mimeType: "audio/pcm;rate=16000" } });
      micChunks += 1;
      if (micChunks === 1) log("first mic chunk sent");
    } catch { /* session may be closing */ }
  };

  // --- screen frames → Gemini (every 2s) ---
  const video = document.createElement("video");
  video.srcObject = screenStream;
  video.muted = true;
  await video.play();
  const canvas = document.createElement("canvas");
  frameTimer = setInterval(() => {
    if (finished) return;
    const w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return;
    // Frames must be readable enough for Gemini to RECOGNIZE an AI chat window
    // (ChatGPT/Claude UI) — 1024px @ 0.4 quality was too blurry to tell, so it
    // missed cheating. 1440px @ 0.72 is legible without congesting the audio.
    const scale = Math.min(1, 1440 / w);
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    const b64 = canvas.toDataURL("image/jpeg", 0.72).split(",")[1];
    try {
      session.sendRealtimeInput({ video: { data: b64, mimeType: "image/jpeg" } });
      frameCount += 1;
      if (frameCount === 1) log(`first screen frame sent (${canvas.width}x${canvas.height})`);
    } catch { /* session may be closing */ }
  }, 1500);
  log("screen frames streaming every 1.5s");

  // Proactive "watch check": while the candidate codes silently there's no speech
  // to trigger a turn, so Gemini wouldn't look at the screen on its own. This nudge
  // makes it INSPECT the screen every tick for BOTH progress AND AI-assistant use.
  // The key to not looping ("I see Claude" over and over): a positive cheating
  // sighting must route to the ENDING action (speak once, then finish_coding), not
  // to commentary — so seeing it resolves the round instead of narrating it.
  watchTimer = setInterval(() => {
    if (finished) return;
    try {
      session.sendClientContent({
        turns: [{
          role: "user",
          parts: [{
            text:
              "[watch check — I may be coding silently] Look carefully at my ENTIRE screen " +
              "right now. FIRST, check for cheating: if you can see an AI chat interface " +
              "OPEN on screen — ChatGPT, Claude, Copilot, Gemini, or Perplexity (its chat " +
              "window or the active tab showing the chat) — that is cheating, so follow your " +
              "ending rule NOW: say the 'that's not allowed' line ONCE, then call " +
              "finish_coding with 'cheating'. Do not merely describe it; end the round. You " +
              "do NOT need to read its text — an AI chat open on screen is enough. OTHERWISE, " +
              "react in one short sentence to the code that is ACTUALLY visible, or if " +
              "there's no editor yet remind me to open one. Never invent progress.",
          }],
        }],
        turnComplete: true,
      });
    } catch { /* session may be closing */ }
  }, 12000);
  log("watch-check nudges every 12s");

  // Hard backstop against any loop: if the round never ends on its own, close it out
  // after CODING_MAX_MS so Liam can't narrate forever.
  const CODING_MAX_MS = 4 * 60 * 1000;
  setTimeout(() => { if (!finished) { log("coding round hit time cap — ending"); finish("done", "(coding round timed out)"); } }, CODING_MAX_MS);

  return { stop: () => finish("done", "(coding round ended)") };
}
