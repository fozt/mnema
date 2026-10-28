package dev.mnema.app.helper;

import java.io.IOException;

/** Recording pause, like WavTrackWriter.paused on desktop: frames of both tracks are dropped together, so the tracks stay in sync. */
final class Pause {
    private volatile boolean paused;
    private long since;
    private long total;

    boolean paused() { return paused; }

    /** Pause or resume at nowMs; false — it already was so. */
    synchronized boolean set(boolean on, long nowMs) {
        if (paused == on) return false;
        if (on) since = nowMs;
        else total += Math.max(0, nowMs - since);
        paused = on;
        return true;
    }

    /** How long the recording has been paused by nowMs, including a pause in progress. */
    synchronized long pausedMs(long nowMs) { return paused ? total + Math.max(0, nowMs - since) : total; }

    synchronized void reset() {
        paused = false;
        since = 0;
        total = 0;
    }

    /** A track frame into the file: dropped while paused, silence for a muted track; returns the number of samples written. */
    static int write(WavWriter wav, boolean paused, boolean on, short[] samples, int count) throws IOException {
        if (paused || count <= 0) return 0;
        Sources.filter(on, samples, count);
        wav.write(samples, count);
        return count;
    }
}
