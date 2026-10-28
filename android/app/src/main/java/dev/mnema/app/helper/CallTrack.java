package dev.mnema.app.helper;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * The other side: AudioPolicy, and in a cellular call where it gives only zeros for 3 s in a row, VOICE_DOWNLINK while the
 * call goes on and AudioPolicy is silent. Same file, timing by AudioPolicy: the file has as many samples as it delivered.
 */
final class CallTrack {
    static final String POLICY = "policy";
    static final String DOWNLINK = "downlink";
    static final long ZEROS_MS = 3000;
    // 3 s of zeros plus a 2 s probe: by 5 s of zeros it is clear whether the fallback records; a silent downlink stays open until sound
    static final long PROBE_MS = 2000;
    // this long without a single sample is a failure, like a read error
    static final long STALL_MS = 3000;
    // after a failure, a new attempt while the call goes on and AudioPolicy is silent
    static final long RETRY_MS = 10000;
    // 0.25 s: downlink may drift this far from AudioPolicy without correction — its chunks arrive off beat
    static final int SLACK = 4000;
    private static final int MAX_ATTEMPTS = 20;

    /** Sample source: AudioRecord, a stub in tests. */
    interface Input {
        /** Samples into buffer without waiting; below zero — an error. */
        int read(short[] buffer);

        void close() throws Exception;
    }

    /** Opens and starts VOICE_DOWNLINK; throws with the cause on failure. */
    interface Downlink {
        Input open() throws Exception;
    }

    /** A VOICE_DOWNLINK attempt for the report: atMs — when, soundSeconds — how many seconds of sound it recorded. */
    static final class Attempt {
        final long atMs;
        private boolean sound;
        private boolean zeros;
        private String error;
        private long soundSeconds;

        Attempt(long atMs) { this.atMs = atMs; }

        /** sound — recorded sound, zeros — gave only zeros, error — failed, interrupted — the call, the recording or the AudioPolicy zeros ended first. */
        String result() {
            if (error != null) return "error";
            if (sound) return "sound";
            return zeros ? "zeros" : "interrupted";
        }

        String error() { return error; }

        long soundSeconds() { return soundSeconds; }
    }

    private final Input policy;
    private final Downlink downlink;
    private final List<Attempt> attempts = new ArrayList<>();
    private short[] spare = new short[0];
    private Input open;
    private Attempt attempt;
    private boolean writing;
    private boolean heard;
    private long quietSinceMs = -1;
    private long probeStartedMs;
    private long lastDataMs;
    private long nextTryMs;
    // how many samples AudioPolicy delivered beyond those written to the file; negative — more were written
    private long lag;
    private volatile String source = POLICY;

    CallTrack(Input policy, Downlink downlink) {
        this.policy = policy;
        this.downlink = downlink;
    }

    /** What records the other side now: POLICY or DOWNLINK. */
    String source() { return source; }

    /** The other side's samples for one step into buffer; kind — CallMode call mode; below zero — AudioPolicy failed. */
    int read(short[] buffer, String kind, long nowMs) {
        if (!CallMode.CELLULAR.equals(kind)) {
            leave();
            return even(buffer, policy.read(buffer));
        }
        if (spare.length != buffer.length) spare = new short[buffer.length];
        int count = policy.read(buffer);
        if (count < 0) return count;
        boolean policySound = Presence.nonzero(buffer, count);
        if (policySound || quietSinceMs < 0) quietSinceMs = nowMs;
        if (open != null && policySound) {
            HelperLog.line("AudioPolicy gives sound again: the other side is recorded from it");
            close();
        }
        if (open == null && !policySound && due(nowMs)) start(nowMs);
        if (open == null) return even(buffer, count);
        int got = open.read(spare);
        if (got < 0) return fail("VOICE_DOWNLINK AudioRecord.read error " + got, buffer, count, nowMs);
        if (got > 0) lastDataMs = nowMs;
        else if (nowMs - lastDataMs >= STALL_MS) return fail("VOICE_DOWNLINK delivers no samples", buffer, count, nowMs);
        boolean sound = Presence.nonzero(spare, got);
        if (!writing) {
            if (sound) {
                writing = true;
                source = DOWNLINK;
                attempt.sound = true;
                HelperLog.line("The other side is recorded from VOICE_DOWNLINK");
            } else {
                if (!attempt.zeros && nowMs - probeStartedMs >= PROBE_MS) {
                    attempt.zeros = true;
                    HelperLog.line("VOICE_DOWNLINK gives only zeros so far: waiting for sound until the call ends");
                }
                return even(buffer, count);
            }
        }
        if (sound) heard = true;
        lag += count - got;
        System.arraycopy(spare, 0, buffer, 0, got);
        return fit(buffer, got, SLACK);
    }

    /** A second of recording has passed: for the report — whether VOICE_DOWNLINK recorded sound in it. */
    void tick() {
        if (writing && heard) attempt.soundSeconds++;
        heard = false;
    }

    /** VOICE_DOWNLINK attempts in order; empty — none were made. */
    List<Attempt> attempts() { return new ArrayList<>(attempts); }

    /** End of the recording or the call: VOICE_DOWNLINK is closed, the other side comes from AudioPolicy again. */
    void close() {
        Input current = open;
        open = null;
        writing = false;
        source = POLICY;
        if (current == null) return;
        try {
            current.close();
        } catch (Exception error) {
            HelperLog.error("VOICE_DOWNLINK did not close", error);
        }
    }

    private boolean due(long nowMs) {
        return attempts.size() < MAX_ATTEMPTS && nowMs - quietSinceMs >= ZEROS_MS && nowMs >= nextTryMs;
    }

    private void start(long nowMs) {
        attempt = new Attempt(nowMs);
        attempts.add(attempt);
        HelperLog.line("AudioPolicy gives only zeros in a cellular call: trying VOICE_DOWNLINK");
        try {
            open = downlink.open();
            probeStartedMs = nowMs;
            lastDataMs = nowMs;
        } catch (Exception error) {
            attempt.error = Failures.describe(error);
            nextTryMs = nowMs + RETRY_MS;
            HelperLog.line("VOICE_DOWNLINK did not open: " + attempt.error);
        }
    }

    // the step is written from AudioPolicy, even fixes the downlink drift from it
    private int fail(String error, short[] buffer, int count, long nowMs) {
        attempt.error = error;
        nextTryMs = nowMs + RETRY_MS;
        HelperLog.line(error + ": the other side is from AudioPolicy again");
        close();
        return even(buffer, count);
    }

    // AudioPolicy samples: the previous downlink drift from it goes to zero
    private int even(short[] buffer, int count) { return count < 0 ? count : fit(buffer, count, 0); }

    // drift beyond tolerance: pad a shortfall with zeros, drop the excess
    private int fit(short[] buffer, int count, long tolerance) {
        if (lag > tolerance) {
            int pad = (int) Math.min(lag, buffer.length - count);
            Arrays.fill(buffer, count, count + pad, (short) 0);
            count += pad;
            lag -= pad;
        } else if (lag < -tolerance) {
            int cut = (int) Math.min(-lag, count);
            count -= cut;
            lag += cut;
        }
        return count;
    }

    // a new call — a new attempt at once, if any are left
    private void leave() {
        close();
        quietSinceMs = -1;
        nextTryMs = 0;
    }
}
