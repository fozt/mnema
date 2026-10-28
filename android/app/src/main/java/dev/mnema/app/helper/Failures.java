package dev.mnema.app.helper;

import java.io.Closeable;
import java.lang.reflect.InvocationTargetException;
import java.util.ArrayList;
import java.util.List;

/** Failures of one recording: an error spoils the recording, a warning does not. */
final class Failures {
    static final String FILES_NOT_CLOSED = "recording files were not saved completely";

    private final List<String> errors = new ArrayList<>();
    private final List<String> warnings = new ArrayList<>();
    private String reason;

    /** The first error sets the failure reason: the stage it happened at. */
    void error(String stage, Throwable cause) {
        errors.add(describe(cause));
        if (reason == null) reason = stage;
    }

    void warning(Throwable cause) { warnings.add(describe(cause)); }

    /** A failure to close a recording file is an error; returns its cause, null — closed. */
    Throwable close(Closeable file) {
        if (file == null) return null;
        try {
            file.close();
            return null;
        } catch (Exception cause) {
            error(FILES_NOT_CLOSED, cause);
            return cause;
        }
    }

    /** The failure reason (English, the app translates it), or null when the recording is intact. */
    String reason() { return reason; }

    List<String> errors() { return new ArrayList<>(errors); }

    List<String> warnings() { return new ArrayList<>(warnings); }

    static Throwable root(Throwable error) {
        while (error instanceof InvocationTargetException && error.getCause() != null) error = error.getCause();
        return error;
    }

    /** Class and message of the root cause. */
    static String describe(Throwable cause) {
        Throwable root = root(cause);
        return root.getClass().getName() + ": " + root.getMessage();
    }
}
