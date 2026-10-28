"""Core: media operations without any UI."""

from .audio import extract_audio, mix
from .backends import AUTO, backend_choices
from .base import MEDIA_EXT, VIDEO_EXT, MediaError, fmt_duration, fmt_size
from .download import clean_url, is_url
from .download import fetch as download_url
from .models import Segment, Transcript
from .probe import MediaInfo, probe
from .transcribe import transcribe
from .video import compress, merge

__all__ = [
    "MediaError",
    "MediaInfo",
    "Segment",
    "Transcript",
    "probe",
    "extract_audio",
    "mix",
    "transcribe",
    "AUTO",
    "backend_choices",
    "is_url",
    "clean_url",
    "download_url",
    "compress",
    "merge",
    "fmt_duration",
    "fmt_size",
    "MEDIA_EXT",
    "VIDEO_EXT",
]
