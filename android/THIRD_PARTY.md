# Third-party code

## scrcpy (Apache License 2.0)

Classes `dev.mnema.app.helper.ShellContext` and `dev.mnema.app.helper.Capture` adapt the hidden
`AudioPolicy` playback-capture setup and the shell `Context` initialization from
[scrcpy](https://github.com/Genymobile/scrcpy) by Genymobile, licensed under the Apache License,
Version 2.0. The full license text is in [`licenses/scrcpy-LICENSE`](licenses/scrcpy-LICENSE).
Sources adapted:

- `server/src/main/java/com/genymobile/scrcpy/audio/AudioPlaybackCapture.java`
- `server/src/main/java/com/genymobile/scrcpy/FakeContext.java`
- `server/src/main/java/com/genymobile/scrcpy/Workarounds.java`

The adaptation captures the voice-communication usage group with `voiceCommunicationCaptureAllowed`
and `ROUTE_FLAG_LOOP_BACK_RENDER`, falls back to the `VOICE_DOWNLINK` source in a cellular call (as
scrcpy's `voice-call-downlink`), records the owner's microphone on a separate track, writes PCM16
WAV and a diagnostic JSON, and explicitly unregisters its audio policy. It does not change
`AudioManager` mode, request audio focus, or choose output devices.

```
   Copyright (C) 2018-2026 Genymobile

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
```
