"""Transcription data models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .base import fmt_stamp, speaker_word


class Segment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None


class Transcript(BaseModel):
    text: str
    segments: list[Segment] = Field(default_factory=list)
    language: str | None = None
    engine: str | None = None  # recognizing engine (gigaam/mlx/whisperx); None if unknown

    def as_text(self, *, paragraph_gap: float = 1.0) -> str:
        """Readable text: segments joined, a new paragraph at a pause ≥ `paragraph_gap` s.

        Without segments returns `text` as is.
        """
        if not self.segments:
            return self.text
        return "\n\n".join(text for _, _, text in self._paragraphs(paragraph_gap))

    def _paragraphs(
        self, paragraph_gap: float, *, by_speaker: bool = False
    ) -> list[tuple[str | None, float, str]]:
        paragraphs: list[tuple[str | None, float, str]] = []
        current: list[str] = []
        speaker: str | None = None
        start = 0.0
        prev_end: float | None = None
        for s in self.segments:
            text = s.text.strip()
            if not text:
                continue
            if current and (
                (by_speaker and s.speaker != speaker)
                or (prev_end is not None and s.start - prev_end >= paragraph_gap)
            ):
                paragraphs.append((speaker, start, " ".join(current)))
                current = []
                prev_end = None
            if not current:
                start = s.start
            speaker = s.speaker
            current.append(text)
            prev_end = max(prev_end, s.end) if prev_end is not None else s.end
        if current:
            paragraphs.append((speaker, start, " ".join(current)))
        return paragraphs

    def with_speakers(self, *, paragraph_gap: float = 1.0, timed: bool = False) -> str:
        """Paragraphs by pauses and track changes; every paragraph repeats its track label.

        One track may hold several people, so a paragraph break does not tell who speaks.
        Without labels the same pause-based split is kept.
        """
        labelled = any(s.speaker for s in self.segments)
        if not self.segments or (not labelled and not timed):
            return self.as_text(paragraph_gap=paragraph_gap)
        unknown = f"{speaker_word(self.language)} ?"
        return "\n\n".join(
            (f"[{fmt_stamp(start)}] " if timed else "")
            + (f"{speaker or unknown}: " if labelled else "")
            + text
            for speaker, start, text in self._paragraphs(paragraph_gap, by_speaker=True)
        )
