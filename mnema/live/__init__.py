"""Live mode (`mnema live`): sources and tracks on the client, recognition and translation on
the server.

Bypasses dispatch/@op because it is latency-critical. Heavy and platform imports (sounddevice,
whisperlivekit) are lazy, so the package imports without them (thin client). Speakers are told
apart by source (AudioSource), not by diarization.
"""
