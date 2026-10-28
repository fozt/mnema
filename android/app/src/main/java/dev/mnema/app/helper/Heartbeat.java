package dev.mnema.app.helper;

/** What the helper writes to status.json. */
final class Heartbeat {
    static final long CALL_END_GRACE_MS = 2L * 60 * 1000;
    static final long RECORDING_NOTICE_MS = 60_000;

    /** Levels of the recording in progress. */
    interface Levels {
        double takeCallRms();

        double takeMicRms();

        Boolean micSilenced();

        /** Sound from the other side arrived in the last 3 s; null — the recording has not started yet. */
        Boolean callSound();

        Boolean micSound();

        /** For how many ms in a row the other side has been zeros, during a call counted from its start; null — not started yet. */
        Long callSilentMs();

        /** What records the other side: CallTrack.POLICY or CallTrack.DOWNLINK; null — the recording has not started yet. */
        String callSource();
    }

    private final int pid;
    private final String nonce;
    private final long apkUpdated;
    private final Sources sources;
    private final Pause pause;
    private String pong = "";
    private boolean recording;
    private String folder = "";
    private String lastFolder = "";
    private String error = "";
    private long startedMs;
    // read by the recording thread too, without the lock
    private volatile String callKind = CallMode.NONE;
    private boolean stopAfterCall;
    private long callEndedUptimeMs = -1;
    private long noticeUptimeMs;
    private boolean interruptedUnannounced;
    private String sound = "";

    Heartbeat(int pid, String nonce, long apkUpdated, Sources sources, Pause pause) {
        this.pid = pid;
        this.nonce = nonce;
        this.apkUpdated = apkUpdated;
        this.sources = sources;
        this.pause = pause;
    }

    static final String FAILED_TO_START = "Couldn't start recording: ";
    static final String STOPPED_BY_ERROR = "Recording stopped because of an error: ";

    /** English, like every helper text in status.json: the helper does not know the app language, the app translates it. */
    static String failure(boolean started, String reason) {
        return (started ? STOPPED_BY_ERROR : FAILED_TO_START) + reason;
    }

    synchronized boolean recording() { return recording; }

    synchronized void pong(String uuid) { pong = uuid; }

    synchronized void error(String text) { error = text; }

    /** The previous helper died while recording name: it is the last one, its stop has not been reported yet. */
    synchronized void interrupted(String name) {
        lastFolder = name;
        interruptedUnannounced = true;
    }

    /** True once after interrupted: time to send RECORDING_STOPPED. */
    synchronized boolean takeInterrupted() {
        boolean due = interruptedUnannounced;
        interruptedUnannounced = false;
        return due;
    }

    /** Call mode CallMode as the helper loop sees it. */
    String callKind() { return callKind; }

    /**
     * A call by the CallMode audio mode; the event for the transition, or null. Not recording: CALL_STARTED — a conversation
     * began, STATE_CHANGED — an incoming call began or ended, a conversation ended. While recording, CALL_ENDED — the
     * conversation or the incoming call the recording started in has ended.
     */
    synchronized String call(String kind, long uptimeMs) {
        String was = callKind;
        if (was.equals(kind)) return null;
        callKind = kind;
        boolean before = CallMode.engaged(was);
        boolean now = CallMode.engaged(kind);
        if (now) callEndedUptimeMs = -1;
        else if (before) callEndedUptimeMs = uptimeMs;
        if (recording) return before && !now && (CallMode.call(was) || stopAfterCall) ? Events.CALL_ENDED : null;
        if (CallMode.call(kind)) return CallMode.call(was) ? null : Events.CALL_STARTED;
        return Events.STATE_CHANGED;
    }

    /** The sound of the tracks in progress changed: the notification checks the status. The first check of a recording is not a change. */
    synchronized boolean soundChanged(Levels levels) {
        String now = recording && levels != null && !folder.isEmpty() ? sound(levels) : "";
        boolean changed = !sound.isEmpty() && !now.isEmpty() && !now.equals(sound);
        sound = now;
        return changed;
    }

    private String sound(Levels levels) {
        Long silent = levels.callSilentMs();
        boolean blocked = CallMode.call(callKind) && silent != null && silent >= HelperProtocol.SILENT_CALL_MS;
        return levels.callSound() + "," + levels.micSound() + "," + blocked;
    }

    synchronized void starting(long nowMs, long uptimeMs) {
        recording = true;
        folder = "";
        error = "";
        startedMs = nowMs;
        stopAfterCall = CallMode.engaged(callKind);
        callEndedUptimeMs = -1;
        noticeUptimeMs = uptimeMs;
        pause.reset();
    }

    synchronized void started(String name) { folder = name; }

    /** Pause or resume by the status clock; only while recording, false — nothing changed. */
    synchronized boolean pause(boolean on, long nowMs) { return recording && pause.set(on, nowMs); }

    synchronized void stopped(String failure) {
        recording = false;
        if (!folder.isEmpty()) lastFolder = folder;
        folder = "";
        stopAfterCall = false;
        pause.reset();
        if (failure != null) error = failure;
    }

    /** True once: the call or incoming call the recording started in ended 2 min ago. */
    synchronized boolean autoStop(long uptimeMs) {
        if (!recording || !stopAfterCall || CallMode.engaged(callKind) || callEndedUptimeMs < 0) return false;
        if (uptimeMs - callEndedUptimeMs < CALL_END_GRACE_MS) return false;
        stopAfterCall = false;
        return true;
    }

    /** While recording, true every RECORDING_NOTICE_MS: repeating RECORDING_STARTED keeps the notification alive. */
    synchronized boolean noticeDue(long uptimeMs) {
        if (!recording || folder.isEmpty() || uptimeMs - noticeUptimeMs < RECORDING_NOTICE_MS) return false;
        noticeUptimeMs = uptimeMs;
        return true;
    }

    synchronized StatusWriter.Status status(long nowMs, long uptimeMs, Levels levels) {
        StatusWriter.Status status = new StatusWriter.Status();
        status.state = recording ? "recording" : "idle";
        status.pid = pid;
        status.nonce = nonce;
        status.apkUpdated = apkUpdated;
        status.heartbeatMs = nowMs;
        status.heartbeatUptimeMs = uptimeMs;
        status.pong = pong;
        status.folder = folder;
        status.lastFolder = lastFolder;
        status.error = error;
        status.startedMs = startedMs;
        status.callMode = callKind;
        status.callActive = CallMode.call(callKind);
        status.micOn = sources.mic();
        status.callOn = sources.call();
        status.paused = pause.paused();
        status.pausedMs = pause.pausedMs(nowMs);
        if (recording && levels != null) {
            status.callRms = levels.takeCallRms();
            status.micRms = levels.takeMicRms();
            status.micSilenced = levels.micSilenced();
            status.callSound = levels.callSound();
            status.micSound = levels.micSound();
            status.callSilentMs = levels.callSilentMs();
            status.callSource = levels.callSource();
        }
        return status;
    }
}
