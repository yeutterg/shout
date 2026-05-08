"""Streaming Parakeet inference with finalized/draft token separation.

The `Streamer` owns one StreamingParakeet for the duration of a single
PTT session. Audio capture (sounddevice InputStream) runs on PortAudio's
internal thread; chunks are drained into MLX inside `tick()`, which
should be called from the daemon's worker thread on a steady cadence
(every ~100 ms). Each tick returns a `Frame` describing what's newly
finalized and what the current draft (still-being-revised) text is.

The driver (daemon) is responsible for:
  - Creating one Streamer per session via `Streamer.start(model)`
  - Polling `tick()` on a worker thread
  - Typing newly-finalized text via `inject.type_text`
  - Updating the overlay with the draft
  - Calling `stop()` and reading the final flush
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Optional

import mlx.core as mx
import numpy as np
import sounddevice as sd

# We hard-code the audio config to what Parakeet expects. mlx-community
# Parakeet TDT 0.6B v3 uses 16 kHz mono. Reading these from the model at
# session start would be cleaner but adds an indirection without value.
_SAMPLE_RATE = 16_000
_CHANNELS = 1
_BLOCKSIZE = 1_600  # 100 ms at 16 kHz

# (left_context, right_context) in encoder frames (~80 ms each for
# parakeet-tdt-0.6b-v3 with subsampling_factor=8). Right context = 8
# means ~640 ms of forward audio is buffered before tokens finalize —
# a deliberate tradeoff between latency and stability. 0 (the PRD's
# original ask) is rejected by parakeet-mlx ≥0.5.
DEFAULT_CONTEXT_SIZE = (256, 8)


@dataclass
class Frame:
    """One tick of streaming output.

    `finalized_delta` is text that was *newly* finalized since the
    previous Frame and should be typed exactly once. `draft` is the
    full current draft (replaces previous draft entirely)."""

    finalized_delta: str
    draft: str


class Streamer:
    def __init__(self, model, context_size: tuple[int, int] = DEFAULT_CONTEXT_SIZE):
        self._model = model
        self._context_size = context_size
        self._stream_ctx = None  # the StreamingParakeet context manager
        self._streamer = None
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue()
        self._sd_stream: Optional[sd.InputStream] = None
        self._finalized_emitted_count = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        """Open audio device and start the streaming context."""
        self._stream_ctx = self._model.transcribe_stream(
            context_size=self._context_size
        )
        self._streamer = self._stream_ctx.__enter__()
        self._finalized_emitted_count = 0

        def _audio_callback(indata, frames, time_info, status):
            # PortAudio thread — keep it cheap. Just push into the queue.
            self._audio_q.put(indata[:, 0].copy())

        self._sd_stream = sd.InputStream(
            samplerate=_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype="float32",
            blocksize=_BLOCKSIZE,
            callback=_audio_callback,
        )
        self._sd_stream.start()

    def tick(self) -> Optional[Frame]:
        """Drain queued audio into the streamer and return the delta.

        Returns None when nothing new arrived. When new audio was
        processed, returns a Frame with whatever (possibly empty) text
        deltas resulted."""
        chunks: list[np.ndarray] = []
        try:
            while True:
                chunks.append(self._audio_q.get_nowait())
        except queue.Empty:
            pass
        if not chunks:
            return None

        audio = np.concatenate(chunks).astype(np.float32)
        self._streamer.add_audio(mx.array(audio))

        return self._build_frame()

    def stop(self) -> Frame:
        """Close audio, drain any last chunks, return the final frame.

        After stop() the Streamer cannot be reused — make a new one
        for the next session."""
        if self._sd_stream is not None:
            self._sd_stream.stop()
            self._sd_stream.close()
            self._sd_stream = None

        # Drain whatever audio arrived between the last tick() and now.
        leftover_frame = self.tick() or Frame(finalized_delta="", draft="")

        # On end-of-session, treat any remaining draft text as final —
        # the user has stopped speaking, so there are no more revisions
        # coming. This is the v0 "final flush" behavior.
        with self._lock:
            draft = self._render_draft()

        # Close the streaming context (frees MLX caches).
        if self._stream_ctx is not None:
            self._stream_ctx.__exit__(None, None, None)
            self._stream_ctx = None
            self._streamer = None

        return Frame(
            finalized_delta=leftover_frame.finalized_delta + draft,
            draft="",
        )

    def _build_frame(self) -> Frame:
        with self._lock:
            finalized_tokens = list(self._streamer.finalized_tokens)
            draft_tokens = list(self._streamer.draft_tokens)

        new_finalized = finalized_tokens[self._finalized_emitted_count :]
        self._finalized_emitted_count = len(finalized_tokens)

        finalized_delta = "".join(t.text for t in new_finalized)
        draft = "".join(t.text for t in draft_tokens)

        return Frame(finalized_delta=finalized_delta, draft=draft)

    def _render_draft(self) -> str:
        if self._streamer is None:
            return ""
        return "".join(t.text for t in self._streamer.draft_tokens)
