package dev.mnema.app.helper;

import java.io.File;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;

/** Events to the app: am broadcast without shell or extras. */
final class Events {
    static final String CALL_STARTED = HelperProtocol.ACTION_CALL_STARTED;
    static final String RECORDING_STARTED = HelperProtocol.ACTION_RECORDING_STARTED;
    static final String RECORDING_STOPPED = HelperProtocol.ACTION_RECORDING_STOPPED;
    static final String CALL_ENDED = HelperProtocol.ACTION_CALL_ENDED;
    static final String STATE_CHANGED = HelperProtocol.ACTION_STATE_CHANGED;
    private static final String RECEIVER = "dev.mnema.app/.notify.HelperEventReceiver";
    private static final long TIMEOUT_S = 5;
    private static final File NULL = new File("/dev/null");

    private final ExecutorService executor = Executors.newSingleThreadExecutor(new Factory());

    void send(String action) {
        try {
            executor.execute(new Task(action));
        } catch (RuntimeException rejected) {
            HelperLog.line("Event " + action + " not sent: the helper is exiting");
        }
    }

    /** Wait for sent events before exiting. */
    void finish(long timeoutMs) {
        executor.shutdown();
        try {
            executor.awaitTermination(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    private static void broadcast(String action) {
        Process process = null;
        try {
            process = new ProcessBuilder("am", "broadcast", "-a", action, "-n", RECEIVER, "--include-stopped-packages")
                    .redirectInput(ProcessBuilder.Redirect.from(NULL))
                    .redirectOutput(ProcessBuilder.Redirect.to(NULL))
                    .redirectError(ProcessBuilder.Redirect.to(NULL))
                    .start();
            if (!process.waitFor(TIMEOUT_S, TimeUnit.SECONDS)) {
                HelperLog.line("am broadcast " + action + ": no answer in " + TIMEOUT_S + " s");
            } else if (process.exitValue() != 0) {
                HelperLog.line("am broadcast " + action + ": exit code " + process.exitValue());
            }
        } catch (Exception error) {
            HelperLog.line("am broadcast " + action + ": " + error);
        } finally {
            if (process != null) process.destroyForcibly();
        }
    }

    private static final class Factory implements ThreadFactory {
        @Override public Thread newThread(Runnable task) {
            Thread thread = new Thread(task, "mnema-helper-events");
            thread.setDaemon(true);
            return thread;
        }
    }

    private static final class Task implements Runnable {
        private final String action;

        Task(String action) { this.action = action; }

        @Override public void run() { broadcast(action); }
    }
}
