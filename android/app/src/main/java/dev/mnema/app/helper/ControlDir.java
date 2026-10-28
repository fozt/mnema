package dev.mnema.app.helper;

import java.io.File;

/** Only the app creates the exchange folder: a folder made by shell is not accessible to its uid. The helper waits for it; one thread. */
final class ControlDir {
    static final long CHECK_MS = 2000;
    static final long REMIND_MS = 60_000;
    static final String MISSING = "No exchange folder: open Mnema";
    private static final long NEVER = -1;

    private final File dir;
    private boolean present;
    private long checked = NEVER;
    private long reminded = NEVER;

    ControlDir(File dir) { this.dir = dir; }

    /** While the folder is missing the file system is checked every CHECK_MS; a folder that disappears is noticed at once. */
    boolean ready(long uptimeMs) {
        if (present || checked == NEVER || uptimeMs - checked >= CHECK_MS) {
            checked = uptimeMs;
            present = dir.isDirectory();
            if (present) reminded = NEVER;
        }
        return present;
    }

    /** Log it: as soon as the folder is gone, then every REMIND_MS. */
    boolean reminderDue(long uptimeMs) {
        if (present || (reminded != NEVER && uptimeMs - reminded < REMIND_MS)) return false;
        reminded = uptimeMs;
        return true;
    }
}
