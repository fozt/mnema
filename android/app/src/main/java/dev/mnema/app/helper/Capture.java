/* Adapted from scrcpy, Copyright (C) 2018-2026 Genymobile, Apache-2.0: see android/THIRD_PARTY.md. */
package dev.mnema.app.helper;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioRecordingConfiguration;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.Closeable;
import java.io.File;
import java.io.IOException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;

/** One recording: the other side — hidden AudioPolicy, VOICE_DOWNLINK while it gives zeros in a cellular call; own voice — AudioRecord(MIC). */
final class Capture implements Heartbeat.Levels {
    static final int RATE = 16000;
    static final String CALL_WAV = "system.wav";
    static final String MIC_WAV = "mic.wav";
    static final String REPORT = HelperProtocol.REPORT;
    // failure stages, in English: the app translates them (HelperTexts.helperText)
    static final String NO_SYSTEM_AUDIO = "no access to system audio";
    static final String MIC_UNAVAILABLE = "microphone unavailable";
    static final String NO_FOLDER_ACCESS = "no access to the Documents/Mnema folder";
    static final String FILES_NOT_CREATED = "recording files can't be created";
    static final String SYSTEM_AUDIO_NOT_RECORDING = "system audio isn't recording";
    static final String MIC_NOT_RECORDING = "microphone isn't recording";
    static final String FILES_NOT_WRITTEN = "recording files can't be written";
    static final String SYSTEM_AUDIO_LOST = "system audio was lost";
    static final String MIC_LOST = "microphone was lost";
    private static final long TICK_MS = 1000;
    private static final int REPORT_EVERY_S = 60;
    private static final int LOG_TAIL_BYTES = 64 * 1024;
    private static final int LOG_TAIL_LINES = 300;
    private static final String[] PERMISSIONS = {
        "android.permission.CAPTURE_AUDIO_OUTPUT",
        "android.permission.CAPTURE_VOICE_COMMUNICATION_OUTPUT",
        "android.permission.MODIFY_AUDIO_ROUTING",
        "android.permission.RECORD_AUDIO",
    };

    /** Recording folder: created once both tracks are ready. */
    interface Folder { File create() throws IOException; }

    /** Current CallMode call mode. */
    interface Call { String kind(); }

    private final Context context;
    private final Sources sources;
    private final Pause pause;
    private final Call call;
    private final JSONObject report = new JSONObject();
    private final Failures failures = new Failures();
    private final Stats callTotal = new Stats();
    private final Stats micTotal = new Stats();
    private final Meter callLevel = new Meter();
    private final Meter micLevel = new Meter();
    private final Presence callPresence = new Presence();
    private final Presence micPresence = new Presence();
    private volatile CallTrack track;
    private volatile long callSinceMs = -1;
    private long seconds;
    private long cellularSeconds;
    private long messengerSeconds;
    private long ringingSeconds;
    private volatile Boolean micSilenced;
    private AudioManager manager;
    private Class<?> policyClass;
    private Object policy;
    private boolean registered;
    private AudioRecord record;
    private AudioRecord mic;
    private WavWriter wav;
    private WavWriter micWav;
    private File dir;
    private boolean reportWritten;
    private String stage = NO_SYSTEM_AUDIO;
    private long entered;
    private volatile long captureStarted;

    Capture(Context context, Sources sources, Pause pause, Call call) {
        this.context = context;
        this.sources = sources;
        this.pause = pause;
        this.call = call;
    }

    @Override public double takeCallRms() { return callLevel.take(); }

    @Override public double takeMicRms() { return micLevel.take(); }

    @Override public Boolean micSilenced() { return micSilenced; }

    @Override public Boolean callSound() { return captureStarted == 0 ? null : callPresence.sound(SystemClock.elapsedRealtime()); }

    @Override public Boolean micSound() { return captureStarted == 0 ? null : micPresence.sound(SystemClock.elapsedRealtime()); }

    @Override public Long callSilentMs() {
        long started = captureStarted;
        if (started == 0) return null;
        long since = callSinceMs;
        return callPresence.silentMs(since >= 0 ? since : started, SystemClock.elapsedRealtime());
    }

    @Override public String callSource() {
        CallTrack current = track;
        return captureStarted == 0 || current == null ? null : current.source();
    }

    /** The recording report, or null when it was not written. */
    File report() { return reportWritten ? new File(dir, REPORT) : null; }

    /** Records until stop or maxMs; the reason of the first error, or null. */
    String run(Folder folder, BooleanSupplier stop, long maxMs, Consumer<File> onStarted) throws Exception {
        entered = SystemClock.elapsedRealtime();
        report.put("schema", 1).put("uid", Process.myUid()).put("pid", Process.myPid())
                .put("manufacturer", Build.MANUFACTURER).put("model", Build.MODEL)
                .put("api", Build.VERSION.SDK_INT).put("release", Build.VERSION.RELEASE)
                .put("fingerprint", Build.FINGERPRINT).put("filter", "voice").put("mic_source", "mic")
                .put("sample_rate", RATE).put("channels", 1).put("encoding", "PCM16")
                .put("max_seconds", maxMs / 1000).put("started_at_unix_ms", System.currentTimeMillis())
                .put("started_at_elapsed_ms", entered).put("route", "LOOP_BACK_RENDER")
                .put("status", "initializing").put("permissions", permissions());
        String reason;
        try {
            reason = record(folder, stop, maxMs, onStarted);
        } catch (Throwable error) {
            reason = "error";
            recordError(error);
        }
        release();
        finish(reason);
        return failures.reason();
    }

    // the shell uid has RECORD_AUDIO; AudioPolicy is open only through the hidden API
    @SuppressLint({"MissingPermission", "PrivateApi"})
    private String record(Folder folder, BooleanSupplier stop, long maxMs, Consumer<File> onStarted) throws Exception {
        if (stop.getAsBoolean()) return "stop_before_start";
        manager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        report.put("audio_mode_before", manager.getMode());
        AudioFormat format = new AudioFormat.Builder().setSampleRate(RATE)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_IN_MONO).build();
        Class<?> ruleClass = Class.forName("android.media.audiopolicy.AudioMixingRule");
        Class<?> ruleBuilderClass = Class.forName("android.media.audiopolicy.AudioMixingRule$Builder");
        Object ruleBuilder = ruleBuilderClass.getConstructor().newInstance();
        ruleBuilderClass.getMethod("setTargetMixRole", int.class).invoke(ruleBuilder, ruleClass.getField("MIX_ROLE_PLAYERS").getInt(null));
        Method addRule = ruleBuilderClass.getMethod("addMixRule", int.class, Object.class);
        int usageRule = ruleClass.getField("RULE_MATCH_ATTRIBUTE_USAGE").getInt(null);
        addRule.invoke(ruleBuilder, usageRule, new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION).build());
        report.put("usages", new JSONArray().put(AudioAttributes.USAGE_VOICE_COMMUNICATION));
        // before build(): the rule keeps the flag value, not the builder itself
        ruleBuilderClass.getMethod("voiceCommunicationCaptureAllowed", boolean.class).invoke(ruleBuilder, true);
        Object rule = ruleBuilderClass.getMethod("build").invoke(ruleBuilder);
        Class<?> mixClass = Class.forName("android.media.audiopolicy.AudioMix");
        Class<?> mixBuilderClass = Class.forName("android.media.audiopolicy.AudioMix$Builder");
        Object mixBuilder = mixBuilderClass.getConstructor(ruleClass).newInstance(rule);
        mixBuilderClass.getMethod("setFormat", AudioFormat.class).invoke(mixBuilder, format);
        mixBuilderClass.getMethod("setRouteFlags", int.class).invoke(mixBuilder, mixClass.getField("ROUTE_FLAG_LOOP_BACK_RENDER").getInt(null));
        Object mix = mixBuilderClass.getMethod("build").invoke(mixBuilder);
        policyClass = Class.forName("android.media.audiopolicy.AudioPolicy");
        Class<?> policyBuilderClass = Class.forName("android.media.audiopolicy.AudioPolicy$Builder");
        Object policyBuilder = policyBuilderClass.getConstructor(Context.class).newInstance(context);
        policyBuilderClass.getMethod("setLooper", Looper.class).invoke(policyBuilder, Looper.getMainLooper());
        policyBuilderClass.getMethod("addMix", mixClass).invoke(policyBuilder, mix);
        policy = policyBuilderClass.getMethod("build").invoke(policyBuilder);
        Method register = AudioManager.class.getDeclaredMethod("registerAudioPolicyStatic", policyClass);
        register.setAccessible(true);
        int result = (int) register.invoke(null, policy);
        report.put("register_result", result);
        if (result != 0) throw new IOException("registerAudioPolicyStatic returned " + result);
        registered = true;
        record = (AudioRecord) policyClass.getMethod("createAudioRecordSink", mixClass).invoke(policy, mix);
        if (record == null || record.getState() != AudioRecord.STATE_INITIALIZED) throw new IOException("AudioRecord not initialized");
        CallTrack other = new CallTrack(new RecordInput(record), new DownlinkRecord(format));
        track = other;
        stage = MIC_UNAVAILABLE;
        mic = new AudioRecord.Builder().setContext(context).setAudioSource(MediaRecorder.AudioSource.MIC).setAudioFormat(format)
                .setBufferSizeInBytes(RATE * 2).build();
        if (mic.getState() != AudioRecord.STATE_INITIALIZED) throw new IOException("Mic AudioRecord not initialized");
        if (stop.getAsBoolean()) return "stop_before_recording";
        stage = NO_FOLDER_ACCESS;
        dir = folder.create();
        stage = FILES_NOT_CREATED;
        wav = new WavWriter(new File(dir, CALL_WAV), RATE);
        micWav = new WavWriter(new File(dir, MIC_WAV), RATE);
        stage = SYSTEM_AUDIO_NOT_RECORDING;
        record.startRecording();
        if (record.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) throw new IOException("AudioRecord did not start");
        stage = MIC_NOT_RECORDING;
        mic.startRecording();
        if (mic.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) throw new IOException("Mic AudioRecord did not start");
        captureStarted = SystemClock.elapsedRealtime();
        report.put("capture_started_at_elapsed_ms", captureStarted).put("setup_ms", captureStarted - entered)
                .put("audio_session_id", record.getAudioSessionId()).put("status", "recording");
        writeReport();
        onStarted.accept(dir);
        stage = FILES_NOT_WRITTEN;
        short[] buffer = new short[4800];
        short[] micBuffer = new short[4800];
        long deadline = captureStarted + maxMs;
        long lastTick = captureStarted;
        while (true) {
            long now = SystemClock.elapsedRealtime();
            if (now >= deadline) return "duration";
            if (stop.getAsBoolean()) return "stop";
            // one value for both tracks: they pause together
            boolean paused = pause.paused();
            String kind = call.kind();
            if (!CallMode.call(kind)) callSinceMs = -1;
            else if (callSinceMs < 0) callSinceMs = now;
            boolean callOn = sources.call();
            // a muted other side needs no fallback
            int count = other.read(buffer, callOn ? kind : CallMode.NONE, now);
            if (count < 0) {
                stage = SYSTEM_AUDIO_LOST;
                throw new IOException("AudioRecord.read error " + count);
            }
            // before writing to the file: a muted track is written as zeros
            callPresence.add(buffer, count, callOn && !paused, now);
            if (Pause.write(wav, paused, callOn, buffer, count) > 0) {
                callTotal.add(buffer, count, callOn);
                callLevel.add(buffer, count);
            }
            int micCount = mic.read(micBuffer, 0, micBuffer.length, AudioRecord.READ_NON_BLOCKING);
            if (micCount < 0) {
                stage = MIC_LOST;
                throw new IOException("Mic AudioRecord.read error " + micCount);
            }
            boolean micOn = sources.mic();
            micPresence.add(micBuffer, micCount, micOn && !paused, now);
            if (Pause.write(micWav, paused, micOn, micBuffer, micCount) > 0) {
                micTotal.add(micBuffer, micCount, micOn);
                micLevel.add(micBuffer, micCount);
            }
            if (count == 0 && micCount == 0) SystemClock.sleep(10);
            if (now - lastTick >= TICK_MS) {
                micSilenced = silenced(mic);
                tick(kind);
                lastTick = now;
            }
        }
    }

    // seconds of recording for the report; the report is rewritten once a minute so a cut-off recording keeps them
    private void tick(String kind) {
        seconds++;
        callPresence.tick();
        micPresence.tick();
        track.tick();
        if (CallMode.CELLULAR.equals(kind)) cellularSeconds++;
        else if (CallMode.MESSENGER.equals(kind)) messengerSeconds++;
        else if (CallMode.RINGING.equals(kind)) ringingSeconds++;
        if (seconds % REPORT_EVERY_S == 0) {
            addSeconds();
            writeReport();
        }
    }

    private void addSeconds() {
        try {
            report.put("seconds", new JSONObject().put("total", seconds).put("call_sound", callPresence.seconds())
                    .put("mic_sound", micPresence.seconds()).put("cellular", cellularSeconds).put("messenger", messengerSeconds)
                    .put("ringing", ringingSeconds));
            JSONArray attempts = new JSONArray();
            CallTrack current = track;
            if (current != null) {
                for (CallTrack.Attempt attempt : current.attempts()) {
                    attempts.put(new JSONObject().put("at_s", (attempt.atMs - captureStarted) / 1000).put("result", attempt.result())
                            .put("error", attempt.error() == null ? JSONObject.NULL : attempt.error())
                            .put("sound_seconds", attempt.soundSeconds()));
                }
            }
            report.put("downlink", attempts);
        } catch (Exception error) {
            note(error);
        }
    }

    // every check is guarded: a custom firmware may not have it
    private JSONObject permissions() throws Exception {
        JSONObject granted = new JSONObject();
        for (String name : PERMISSIONS) {
            String key = name.substring(name.lastIndexOf('.') + 1);
            try {
                granted.put(key, context.checkSelfPermission(name) == PackageManager.PERMISSION_GRANTED);
            } catch (Throwable error) {
                granted.put(key, JSONObject.NULL);
                note(error);
            }
        }
        return granted;
    }

    private void release() {
        closeFile(micWav);
        closeFile(wav);
        // files are closed: failures below do not spoil the recording
        CallTrack current = track;
        if (current != null) current.close();
        if (record != null) {
            try { addAudioState(report, record); } catch (Throwable error) { note(error); }
            try {
                if (record.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) record.stop();
            } catch (Throwable error) { note(error); }
            try { record.release(); } catch (Throwable error) { note(error); }
        }
        if (mic != null) {
            try { report.put("mic_total", micTotal.json()); addAudioState(report.getJSONObject("mic_total"), mic); } catch (Throwable error) { note(error); }
            try {
                if (mic.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) mic.stop();
            } catch (Throwable error) { note(error); }
            try { mic.release(); } catch (Throwable error) { note(error); }
        }
        if (registered) {
            try {
                Method unregister = AudioManager.class.getDeclaredMethod("unregisterAudioPolicy", policyClass);
                unregister.setAccessible(true);
                unregister.invoke(manager, policy);
                report.put("policy_unregistered", true);
            } catch (Throwable error) { note(error); }
        }
    }

    private void closeFile(Closeable file) {
        Throwable error = failures.close(file);
        if (error != null) HelperLog.error("Recording: " + Failures.FILES_NOT_CLOSED, error);
    }

    private void finish(String reason) {
        if (dir == null) return;
        try {
            long ended = SystemClock.elapsedRealtime();
            report.put("status", failures.reason() == null ? "stopped" : "error")
                    .put("stop_reason", reason).put("total", callTotal.json())
                    .put("finished_at_unix_ms", System.currentTimeMillis())
                    .put("elapsed_ms", ended - entered)
                    .put("capture_elapsed_ms", captureStarted == 0 ? 0 : ended - captureStarted);
        } catch (Exception error) {
            HelperLog.error("Report summary not built", error);
        }
        if (captureStarted != 0) addSeconds();
        writeReport();
        writeLogTail();
    }

    // the app cannot read the helper log: its tail goes next to the report
    private void writeLogTail() {
        try {
            byte[] tail = HelperLog.tail(new File(HelperProtocol.LOG), LOG_TAIL_BYTES, LOG_TAIL_LINES);
            if (tail != null) StatusWriter.writeAtomic(new File(dir, HelperProtocol.LOG_TAIL), tail, false);
        } catch (Exception error) {
            HelperLog.error("Not written: " + HelperProtocol.LOG_TAIL, error);
        }
    }

    private void addAudioState(JSONObject json, AudioRecord audio) throws Exception {
        if (manager != null) json.put("audio_mode", manager.getMode());
        AudioDeviceInfo device = audio.getRoutedDevice();
        json.put("routed_device", device == null ? JSONObject.NULL : new JSONObject()
                .put("id", device.getId()).put("type", device.getType()));
        AudioRecordingConfiguration configuration = audio.getActiveRecordingConfiguration();
        json.put("client_silenced", configuration == null ? JSONObject.NULL : configuration.isClientSilenced());
        json.put("recording_state", audio.getRecordingState());
    }

    private static Boolean silenced(AudioRecord audio) {
        AudioRecordingConfiguration configuration = audio.getActiveRecordingConfiguration();
        return configuration == null ? null : configuration.isClientSilenced();
    }

    private void recordError(Throwable error) {
        failures.error(stage, error);
        HelperLog.error("Recording: " + stage, error);
    }

    private void note(Throwable error) {
        failures.warning(error);
        HelperLog.error("Recording diagnostics", error);
    }

    // a report failure does not interrupt the recording
    private void writeReport() {
        try {
            report.put("errors", new JSONArray(failures.errors())).put("warnings", new JSONArray(failures.warnings()));
            // the reason for the remark; the exception class stays in errors and the log
            if (failures.reason() != null) report.put("error_reason", failures.reason());
            StatusWriter.writeAtomic(new File(dir, REPORT), (report.toString(2) + "\n").getBytes(StandardCharsets.UTF_8), false);
            reportWritten = true;
        } catch (Exception error) {
            HelperLog.error("Not written: " + REPORT, error);
        }
    }

    /** Track summary for the report; muted — silence of a muted track. */
    private static final class Stats {
        long samples;
        long nonzero;
        long muted;
        double squares;
        int peak;

        void add(short[] data, int count, boolean on) {
            for (int i = 0; i < count; i++) {
                int sample = data[i];
                if (sample != 0) nonzero++;
                peak = Math.max(peak, Math.abs(sample));
                squares += (double) sample * sample;
            }
            samples += count;
            if (!on) muted += count;
        }

        JSONObject json() throws Exception {
            return new JSONObject().put("samples", samples).put("audio_seconds", samples / (double) RATE)
                    .put("nonzero_samples", nonzero).put("zero_samples", samples - nonzero).put("muted_samples", muted)
                    .put("nonzero_fraction", samples == 0 ? 0.0 : nonzero / (double) samples)
                    .put("rms_pcm", samples == 0 ? 0.0 : Math.sqrt(squares / samples))
                    .put("peak_pcm", peak).put("all_zero", samples > 0 && nonzero == 0);
        }
    }

    private static final class RecordInput implements CallTrack.Input {
        private final AudioRecord audio;

        RecordInput(AudioRecord audio) { this.audio = audio; }

        @Override public int read(short[] buffer) { return audio.read(buffer, 0, buffer.length, AudioRecord.READ_NON_BLOCKING); }

        @Override public void close() {
            try {
                if (audio.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) audio.stop();
            } finally {
                audio.release();
            }
        }
    }

    /** VOICE_DOWNLINK — the other side of a cellular call: the shell uid has CAPTURE_AUDIO_OUTPUT, like scrcpy voice-call-downlink. */
    private final class DownlinkRecord implements CallTrack.Downlink {
        private final AudioFormat format;

        DownlinkRecord(AudioFormat format) { this.format = format; }

        @SuppressLint("MissingPermission")
        @Override public CallTrack.Input open() throws Exception {
            AudioRecord audio = new AudioRecord.Builder().setContext(context).setAudioSource(MediaRecorder.AudioSource.VOICE_DOWNLINK)
                    .setAudioFormat(format).setBufferSizeInBytes(RATE * 2).build();
            try {
                if (audio.getState() != AudioRecord.STATE_INITIALIZED) throw new IOException("VOICE_DOWNLINK AudioRecord not initialized");
                audio.startRecording();
                if (audio.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) throw new IOException("VOICE_DOWNLINK AudioRecord did not start");
            } catch (Exception error) {
                try { audio.release(); } catch (Exception released) { error.addSuppressed(released); }
                throw error;
            }
            return new RecordInput(audio);
        }
    }

    /** RMS for status.json since the previous poll. */
    private static final class Meter {
        private double squares;
        private long count;

        synchronized void add(short[] data, int length) {
            for (int i = 0; i < length; i++) squares += (double) data[i] * data[i];
            count += length;
        }

        synchronized double take() {
            double rms = count == 0 ? 0.0 : Math.sqrt(squares / count);
            squares = 0;
            count = 0;
            return rms;
        }
    }
}
