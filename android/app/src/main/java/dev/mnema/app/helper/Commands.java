package dev.mnema.app.helper;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** command.txt: strictly "<verb> <uuid>", no paths or arguments. */
final class Commands {
    static final String FILE = "command.txt";
    static final int MAX_BYTES = 128;
    private static final Pattern COMMAND = Pattern.compile("^(start|stop|ping|mic-on|mic-off|call-on|call-off|pause|resume) ([0-9a-f-]{36})$");

    private Commands() { }

    /** The command verb, or null when the line is not a command. */
    static String parse(String raw) {
        Matcher matcher = match(raw);
        return matcher == null ? null : matcher.group(1);
    }

    static String uuid(String raw) {
        Matcher matcher = match(raw);
        return matcher == null ? null : matcher.group(2);
    }

    /** At most MAX_BYTES + 1 bytes; no file — an empty string. */
    static String read(File file) throws IOException {
        byte[] buffer = new byte[MAX_BYTES + 1];
        int length = 0;
        try (InputStream stream = new FileInputStream(file)) {
            int count;
            while (length < buffer.length && (count = stream.read(buffer, length, buffer.length - length)) > 0) {
                length += count;
            }
        } catch (FileNotFoundException missing) {
            return "";
        }
        return new String(buffer, 0, length, StandardCharsets.UTF_8);
    }

    private static Matcher match(String raw) {
        if (raw == null || raw.getBytes(StandardCharsets.UTF_8).length > MAX_BYTES) return null;
        Matcher matcher = COMMAND.matcher(raw);
        return matcher.matches() ? matcher : null;
    }
}
