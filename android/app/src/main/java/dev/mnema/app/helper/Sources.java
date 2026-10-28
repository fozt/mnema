package dev.mnema.app.helper;

import java.util.Arrays;

/** Which tracks are recorded with sound; a muted one is still read and written as zeros of the same length, like muted on desktop. */
final class Sources {
    private volatile boolean mic = true;
    private volatile boolean call = true;

    boolean mic() { return mic; }

    boolean call() { return call; }

    /** mic-on, mic-off, call-on, call-off; false — the verb is not about tracks. */
    boolean apply(String verb) {
        switch (verb) {
            case "mic-on":
                mic = true;
                return true;
            case "mic-off":
                mic = false;
                return true;
            case "call-on":
                call = true;
                return true;
            case "call-off":
                call = false;
                return true;
            default:
                return false;
        }
    }

    /** The first count samples for the file: silence for a muted track. */
    static void filter(boolean on, short[] samples, int count) {
        if (!on) Arrays.fill(samples, 0, count, (short) 0);
    }
}
