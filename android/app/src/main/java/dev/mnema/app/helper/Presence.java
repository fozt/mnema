package dev.mnema.app.helper;

/** Whether sound reaches a track: non-zero samples in the last 3 s; seconds with sound in the file go to the report. */
final class Presence {
    static final long WINDOW_MS = 3000;
    private static final long NEVER = Long.MIN_VALUE;

    private long lastSoundMs = NEVER;
    private boolean heard;
    private long seconds;

    /** Samples arrived at nowMs; written — they went into the file: the track is on and not paused. */
    synchronized void add(short[] data, int count, boolean written, long nowMs) {
        if (!nonzero(data, count)) return;
        lastSoundMs = nowMs;
        if (written) heard = true;
    }

    synchronized boolean sound(long nowMs) { return lastSoundMs != NEVER && nowMs - lastSoundMs < WINDOW_MS; }

    /** For how many ms in a row there have been only zeros, counting no earlier than sinceMs. */
    synchronized long silentMs(long sinceMs, long nowMs) { return Math.max(0, nowMs - Math.max(lastSoundMs, sinceMs)); }

    /** A second of recording has passed: whether it had sound in the file. */
    synchronized boolean tick() {
        boolean was = heard;
        if (was) seconds++;
        heard = false;
        return was;
    }

    synchronized long seconds() { return seconds; }

    static boolean nonzero(short[] data, int count) {
        for (int i = 0; i < count; i++) {
            if (data[i] != 0) return true;
        }
        return false;
    }
}
