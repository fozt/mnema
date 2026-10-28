package dev.mnema.app.helper;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** A recording cut off together with the previous helper: its status.json was left in state=recording. */
final class Interrupted {
    private static final long MAX_BYTES = 8192;
    // StatusWriter format: no spaces, '"' inside strings is escaped, ',"' occurs only between fields
    private static final Pattern VERSION = Pattern.compile("[{,]\"v\":" + StatusWriter.VERSION + "[,}]");
    private static final Pattern RECORDING = Pattern.compile("[{,]\"state\":\"recording\"[,}]");
    private static final Pattern FOLDER = Pattern.compile("[{,]\"folder\":\"((?:[^\"\\\\]|\\\\.)*)\"[,}]");

    private Interrupted() { }

    /** Folder name of the interrupted recording from status.json in dir; null when nothing was recording, there is no folder yet, or the file is unreadable. */
    static String folder(File dir) {
        File file = new File(dir, StatusWriter.FILE);
        String json;
        try {
            if (file.length() > MAX_BYTES) return null;
            json = new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8);
        } catch (IOException | RuntimeException unreadable) {
            return null;
        }
        if (!VERSION.matcher(json).find() || !RECORDING.matcher(json).find()) return null;
        Matcher folder = FOLDER.matcher(json);
        if (!folder.find()) return null;
        String name = unescape(folder.group(1));
        return isName(name) ? name : null;
    }

    private static boolean isName(String name) {
        return name != null && !name.isEmpty() && !name.equals(".") && !name.equals("..")
                && name.indexOf('/') < 0 && name.indexOf('\0') < 0;
    }

    /** A JSON string without quotes; a broken escape — null. */
    private static String unescape(String raw) {
        StringBuilder out = new StringBuilder(raw.length());
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            if (c != '\\') {
                out.append(c);
                continue;
            }
            char escaped = raw.charAt(++i);
            switch (escaped) {
                case 'b': out.append('\b'); break;
                case 'f': out.append('\f'); break;
                case 'n': out.append('\n'); break;
                case 'r': out.append('\r'); break;
                case 't': out.append('\t'); break;
                case 'u':
                    if (i + 4 >= raw.length()) return null;
                    try {
                        out.append((char) Integer.parseInt(raw.substring(i + 1, i + 5), 16));
                    } catch (NumberFormatException broken) {
                        return null;
                    }
                    i += 4;
                    break;
                default: out.append(escaped); break;
            }
        }
        return out.toString();
    }
}
