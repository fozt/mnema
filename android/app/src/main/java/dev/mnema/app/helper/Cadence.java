package dev.mnema.app.helper;

/** Loop tick: commands every 300 ms, status every 2 s when idle. */
final class Cadence {
    static final long TICK_MS = 300;
    static final long IDLE_STATUS_MS = 2000;
    private static final long NEVER = -1;

    private long lastStatus = NEVER;

    /** While recording the status is due every tick, when idle every IDLE_STATUS_MS. */
    synchronized boolean statusDue(boolean recording, long uptimeMs) {
        return recording || lastStatus == NEVER || uptimeMs - lastStatus >= IDLE_STATUS_MS;
    }

    synchronized void statusWritten(long uptimeMs) { lastStatus = uptimeMs; }
}
