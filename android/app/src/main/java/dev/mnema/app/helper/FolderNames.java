package dev.mnema.app.helper;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;

/** Recording folder names like live_base and free_name on desktop (fixture folder-names-v1): in its own process, without app code. */
final class FolderNames {
    private static final int MAX_BYTES = 255;

    private FolderNames() { }

    static String defaultName(long epochMs, TimeZone tz) {
        SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd_HH-mm", Locale.ROOT);
        format.setTimeZone(tz);
        return format.format(new Date(epochMs));
    }

    /** A taken name becomes "… (2)", "… (3)", at most 255 bytes; symlinks are not followed. */
    static File free(File root, String name) {
        File path = new File(root, name);
        for (int n = 2; Files.exists(path.toPath(), LinkOption.NOFOLLOW_LINKS); n++) {
            String suffix = " (" + n + ")";
            String stem = name;
            while ((stem + suffix).getBytes(StandardCharsets.UTF_8).length > MAX_BYTES && stem.codePointCount(0, stem.length()) > 1) {
                stem = stem.substring(0, stem.offsetByCodePoints(stem.length(), -1));
            }
            path = new File(root, stem + suffix);
        }
        return path;
    }
}
