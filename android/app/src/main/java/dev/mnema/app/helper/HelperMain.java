package dev.mnema.app.helper;

import android.annotation.SuppressLint;
import android.content.Context;
import android.media.AudioManager;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;

import java.io.File;
import java.io.IOException;
import java.util.TimeZone;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;

/** The helper with the shell uid: commands, heartbeat and recording; no lambdas. */
public final class HelperMain {
    @SuppressLint("SdCardPath")
    private static final File CONTROL = new File("/sdcard/Android/data/dev.mnema.app/files", HelperProtocol.CONTROL_DIR);
    private static final File RECORDINGS = new File(HelperProtocol.RECORDINGS_ROOT);
    private static final long MAX_RECORDING_MS = 6L * 3600 * 1000;
    static final String UNKNOWN_COMMAND = "The helper didn't understand the command. Restart it";
    static final String APP_UPDATED = "The helper stopped: the app was updated";
    static final String INTERNAL_FAILURE = "helper failure, restart it";

    private final File sourceDir;
    private final Context context;
    private final Sources sources;
    private final Pause pause;
    private final Heartbeat heartbeat;
    private final Cadence cadence = new Cadence();
    private final ControlDir control = new ControlDir(CONTROL);
    private final Events events = new Events();
    private final AtomicBoolean stopRecording = new AtomicBoolean();
    private volatile Capture capture;
    private volatile Thread recorder;
    private String handled;
    private String rejected = "";
    private String loopFailure = "";
    private boolean statusFailing;

    private HelperMain(File sourceDir, Context context, Sources sources, Pause pause, Heartbeat heartbeat) {
        this.sourceDir = sourceDir;
        this.context = context;
        this.sources = sources;
        this.pause = pause;
        this.heartbeat = heartbeat;
    }

    @SuppressWarnings("deprecation") // AudioPolicy needs the main Looper
    public static void main(String[] args) throws Exception {
        if (Process.myUid() != ShellContext.SHELL_UID) throw new SecurityException("Run under shell UID 2000, actual " + Process.myUid());
        if (args.length != 3) throw new IllegalArgumentException("Usage: HelperMain sourceDir lastUpdateTime nonce");
        File sourceDir = new File(args[0]);
        long apkUpdated = Long.parseLong(args[1]);
        String nonce = args[2];
        HelperLog.line("Helper started: pid " + Process.myPid() + ", nonce " + nonce + ", APK " + apkUpdated);
        if (Looper.getMainLooper() == null) Looper.prepareMainLooper();
        Context context = ShellContext.create();
        // the track choice lives until the helper restarts: a new one gets it from the app
        Sources sources = new Sources();
        Pause pause = new Pause();
        Heartbeat heartbeat = new Heartbeat(Process.myPid(), nonce, apkUpdated, sources, pause);
        HelperMain helper = new HelperMain(sourceDir, context, sources, pause, heartbeat);
        Runtime.getRuntime().addShutdownHook(new Thread(new Shutdown(helper), "mnema-helper-shutdown"));
        Thread control = new Thread(new Control(helper), "mnema-helper-control");
        control.start();
        Looper.loop();
    }

    private void loop() {
        AudioManager manager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        // a command left over from before the start is not executed
        handled = Commands.uuid(readCommand());
        recoverInterrupted();
        while (true) {
            boolean exchange = control.ready(SystemClock.uptimeMillis());
            try {
                if (!sourceDir.exists()) {
                    exitUpdated();
                    return;
                }
                if (exchange) handleCommand();
                else if (control.reminderDue(SystemClock.uptimeMillis())) HelperLog.line(ControlDir.MISSING);
                onCall(manager == null ? CallMode.NONE : CallMode.of(manager.getMode()));
                if (heartbeat.autoStop(SystemClock.uptimeMillis())) {
                    HelperLog.line("The call ended 2 min ago: stopping the recording");
                    stopRecording.set(true);
                }
                if (heartbeat.noticeDue(SystemClock.uptimeMillis())) events.send(Events.RECORDING_STARTED);
                if (heartbeat.soundChanged(capture)) {
                    writeStatus();
                    events.send(Events.STATE_CHANGED);
                }
                loopFailure = "";
            } catch (Exception failure) {
                if (!failure.toString().equals(loopFailure)) HelperLog.error("Helper loop failure", failure);
                loopFailure = failure.toString();
            }
            if (exchange && cadence.statusDue(heartbeat.recording(), SystemClock.uptimeMillis())) writeStatus();
            SystemClock.sleep(Cadence.TICK_MS);
        }
    }

    private void recoverInterrupted() {
        String folder = Interrupted.folder(CONTROL);
        if (folder == null) return;
        HelperLog.line("Recording " + folder + " was cut off with the previous helper: it goes to processing");
        heartbeat.interrupted(folder);
    }

    private void exitUpdated() {
        HelperLog.line("No " + sourceDir + ": the app was updated or removed, exiting");
        stopRecording.set(true);
        joinRecorder(5000);
        heartbeat.error(APP_UPDATED);
        writeStatus();
        events.finish(5000);
    }

    private void onCall(String kind) {
        if (!kind.equals(heartbeat.callKind())) HelperLog.line("Call mode: " + kind);
        String event = heartbeat.call(kind, SystemClock.uptimeMillis());
        if (event == null) return;
        writeStatus();
        events.send(event);
    }

    private void handleCommand() {
        String raw = readCommand();
        String verb = Commands.parse(raw);
        if (verb == null) {
            if (!raw.isEmpty()) reject(raw);
            return;
        }
        String uuid = Commands.uuid(raw);
        if (uuid.equals(handled)) return;
        handled = uuid;
        HelperLog.line("Command " + verb + " " + uuid);
        switch (verb) {
            case "start":
                start();
                break;
            case "stop":
                stopRecording.set(true);
                break;
            case "ping":
                heartbeat.pong(uuid);
                writeStatus();
                break;
            case "pause":
            case "resume":
                if (heartbeat.pause("pause".equals(verb), System.currentTimeMillis())) writeStatus();
                break;
            default:
                if (sources.apply(verb)) writeStatus();
                break;
        }
    }

    private void reject(String raw) {
        heartbeat.error(UNKNOWN_COMMAND);
        if (raw.equals(rejected)) return;
        rejected = raw;
        HelperLog.line("Unknown command in " + Commands.FILE + ", " + raw.length() + " chars");
    }

    private synchronized void start() {
        if (recorder != null) {
            HelperLog.line("Already recording");
            return;
        }
        stopRecording.set(false);
        heartbeat.starting(System.currentTimeMillis(), SystemClock.uptimeMillis());
        Capture current = new Capture(context, sources, pause, new CurrentCall(heartbeat));
        capture = current;
        Thread thread = new Thread(new Recorder(this, current), "mnema-helper-recorder");
        thread.start();
        recorder = thread;
        writeStatus();
    }

    private void record(Capture current) {
        Started started = new Started(this);
        String error = null;
        try {
            String reason = current.run(new NewFolder(), new StopFlag(stopRecording), MAX_RECORDING_MS, started);
            if (reason != null) error = Heartbeat.failure(started.sent, reason);
        } catch (Exception failure) {
            HelperLog.error("Recording failure", failure);
            error = Heartbeat.failure(started.sent, INTERNAL_FAILURE);
        } finally {
            finished(error);
        }
        String report = reportPath(current);
        if (error != null) HelperLog.line(report == null ? error : error + ". Report: " + report);
        // files are closed, status.json is already idle
        if (started.sent) events.send(Events.RECORDING_STOPPED);
    }

    private synchronized void finished(String error) {
        heartbeat.stopped(error);
        capture = null;
        writeStatus();
        recorder = null;
    }

    private void onStarted(File dir) {
        heartbeat.started(dir.getName());
        writeStatus();
        events.send(Events.RECORDING_STARTED);
    }

    private static String reportPath(Capture current) {
        File report = current.report();
        return report == null ? null : report.getPath();
    }

    private File createFolder() throws IOException {
        if (!RECORDINGS.isDirectory() && !RECORDINGS.mkdirs()) throw new IOException("Cannot create " + RECORDINGS);
        File marker = new File(RECORDINGS, ".nomedia");
        try {
            if (!marker.exists() && !marker.createNewFile() && !marker.exists()) HelperLog.line("Not created: " + marker);
        } catch (IOException failure) {
            HelperLog.error("Not created: " + marker, failure);
        }
        File dir = FolderNames.free(RECORDINGS, FolderNames.defaultName(System.currentTimeMillis(), TimeZone.getDefault()));
        if (!dir.mkdirs()) throw new IOException("Cannot create " + dir);
        return dir;
    }

    private String readCommand() {
        try {
            return Commands.read(new File(CONTROL, Commands.FILE)).trim();
        } catch (IOException unreadable) {
            return "";
        }
    }

    private synchronized void writeStatus() {
        if (!CONTROL.isDirectory()) return;
        long uptime = SystemClock.uptimeMillis();
        cadence.statusWritten(uptime);
        try {
            StatusWriter.write(CONTROL, heartbeat.status(System.currentTimeMillis(), uptime, capture));
            statusFailing = false;
            if (heartbeat.takeInterrupted()) events.send(Events.RECORDING_STOPPED);
        } catch (Exception failure) {
            if (!statusFailing) HelperLog.error("Not written: " + StatusWriter.FILE, failure);
            statusFailing = true;
        }
    }

    private void onShutdown() {
        // only after System.exit: SIGTERM in ART kills the process at once, without a shutdown hook
        stopRecording.set(true);
        joinRecorder(3000);
        events.finish(3000);
    }

    private void joinRecorder(long timeoutMs) {
        Thread running = recorder;
        if (running == null) return;
        try {
            running.join(timeoutMs);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    private static final class Control implements Runnable {
        private final HelperMain helper;

        Control(HelperMain helper) { this.helper = helper; }

        @Override public void run() {
            try {
                helper.loop();
            } catch (Throwable fatal) {
                HelperLog.error("Helper stopped", fatal);
            }
            System.exit(0);
        }
    }

    private static final class Shutdown implements Runnable {
        private final HelperMain helper;

        Shutdown(HelperMain helper) { this.helper = helper; }

        @Override public void run() { helper.onShutdown(); }
    }

    private static final class CurrentCall implements Capture.Call {
        private final Heartbeat heartbeat;

        CurrentCall(Heartbeat heartbeat) { this.heartbeat = heartbeat; }

        @Override public String kind() { return heartbeat.callKind(); }
    }

    private static final class StopFlag implements BooleanSupplier {
        private final AtomicBoolean flag;

        StopFlag(AtomicBoolean flag) { this.flag = flag; }

        @Override public boolean getAsBoolean() { return flag.get(); }
    }

    private static final class Recorder implements Runnable {
        private final HelperMain helper;
        private final Capture capture;

        Recorder(HelperMain helper, Capture capture) {
            this.helper = helper;
            this.capture = capture;
        }

        @Override public void run() { helper.record(capture); }
    }

    private final class NewFolder implements Capture.Folder {
        @Override public File create() throws IOException { return createFolder(); }
    }

    private static final class Started implements Consumer<File> {
        private final HelperMain helper;
        private boolean sent;

        Started(HelperMain helper) { this.helper = helper; }

        @Override public void accept(File dir) {
            helper.onStarted(dir);
            sent = true;
        }
    }
}
