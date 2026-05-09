"""Streaming Parakeet inference with finalized/draft token separation.

The `Streamer` owns one StreamingParakeet for the duration of a single
PTT session. Audio capture (sounddevice InputStream) runs on PortAudio's
internal thread; chunks are drained into MLX inside `tick()`, which
should be called from the daemon's worker thread on a steady cadence
(every ~33 ms). Each tick returns a `Frame` describing what's newly
finalized and what the current draft (still-being-revised) text is.

The driver (daemon) is responsible for:
  - Creating one Streamer per session via `Streamer.start(model)`
  - Polling `tick()` on a worker thread
  - Typing newly-finalized text via `inject.type_text`
  - Updating the overlay with the draft
  - Calling `stop()` and reading the final flush
"""

from __future__ import annotations

import logging
import os
import queue
import tempfile
from dataclasses import dataclass
from typing import Optional

import mlx.core as mx
import numpy as np
import sounddevice as sd
import soundfile as sf

log = logging.getLogger("shout.stream")

# Parakeet TDT 0.6B v3 expects 16 kHz mono float32. Reading these from
# the model at session start would be cleaner but adds an indirection
# without value (parakeet-mlx v3 is the one supported model).
_SAMPLE_RATE = 16_000
_CHANNELS = 1
_BLOCKSIZE = 1_600  # 100 ms at 16 kHz

# (left_context, right_context) in encoder frames (~80 ms each for
# parakeet-tdt-0.6b-v3 with subsampling_factor=8). Right context = 16
# means ~1.28 s of forward audio is buffered before tokens finalize —
# a deliberate tradeoff between latency and stability. 0 (the PRD's
# original ask) is rejected by parakeet-mlx ≥0.5; 8 was the v0 starting
# point but produced visibly noisy finalization (the user reported
# "transcription doesn't seem very accurate"); 16 is still inside the
# 1.5 s acceptance budget while giving the model meaningfully more
# future context.
DEFAULT_CONTEXT_SIZE = (256, 16)


@dataclass
class Frame:
    """One tick of streaming output.

    `finalized_delta` is text that was *newly* finalized since the
    previous Frame and should be typed exactly once. `draft` is the
    full current draft (replaces previous draft entirely)."""

    finalized_delta: str
    draft: str


class Streamer:
    def __init__(
        self,
        model,
        context_size: tuple[int, int] = DEFAULT_CONTEXT_SIZE,
        input_device: str | None = None,
    ):
        self._model = model
        self._context_size = context_size
        # sounddevice accepts a device name or None for the system default.
        # If the user picks a name and later unplugs that device,
        # InputStream.start() raises and the session fails-soft (the
        # daemon logs and returns to idle).
        self._input_device = input_device
        self._stream_ctx = None  # the StreamingParakeet context manager
        self._streamer = None
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue()
        self._sd_stream: Optional[sd.InputStream] = None
        self._finalized_emitted_count = 0
        self._started = False
        # Full audio buffer captured during this session, retained for
        # batch_transcribe() after stop(). list-of-arrays so we avoid
        # repeated reallocation during long sessions.
        self._audio_log: list[np.ndarray] = []

    def start(self) -> None:
        """Open audio device and start the streaming context.

        A Streamer instance is single-use; calling start() twice raises."""
        if self._started:
            raise RuntimeError("Streamer.start() may only be called once")
        self._started = True

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
            device=self._input_device,
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
        # Retain a copy for the post-release batch transcribe.
        self._audio_log.append(audio)
        self._streamer.add_audio(mx.array(audio))

        return self._build_frame()

    def stop(self) -> Frame:
        """Close audio capture and the streaming context. Returns the
        last streaming frame (for the live overlay); the canonical
        transcription comes from a separate batch_transcribe() call.

        The audio log is retained so batch_transcribe() can run
        full-context inference over the entire session afterwards.
        """
        if self._sd_stream is not None:
            self._sd_stream.stop()
            self._sd_stream.close()
            self._sd_stream = None

        # Drain any final chunks (also updates the live overlay one
        # last time before we tear down).
        leftover = self.tick() or Frame(finalized_delta="", draft="")

        if self._stream_ctx is not None:
            self._stream_ctx.__exit__(None, None, None)
            self._stream_ctx = None
            self._streamer = None

        return leftover

    def batch_transcribe(self) -> str:
        """Run a full-context (non-streaming) batch transcribe over the
        audio captured during this session.

        Streaming gives live feedback but compromises on accuracy
        because each token only sees ~1.3 s of right-context. Batch
        sees the entire utterance in both directions, which is the
        same model in a more accurate configuration. We only run this
        once per session, after the user releases Caps Lock.

        parakeet-mlx's `transcribe()` takes a file path (it uses ffmpeg
        internally to load+resample), so we write the captured audio
        to a temp WAV at the model's native rate and hand off the path.
        """
        if not self._audio_log:
            return ""
        full = np.concatenate(self._audio_log).astype(np.float32)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            sf.write(tmp_path, full, samplerate=_SAMPLE_RATE)
            result = self._model.transcribe(tmp_path)
            text = getattr(result, "text", "") or ""
            log.info(
                "batch transcribed %.2f s of audio → %d chars",
                len(full) / _SAMPLE_RATE, len(text),
            )
            return text
        except Exception:
            log.exception("batch transcribe failed")
            return ""
        finally:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

    def _build_frame(self) -> Frame:
        # Single-threaded: tick() (and therefore _build_frame) is only
        # called from the daemon's worker thread, which also owns
        # add_audio. No locking needed.
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
