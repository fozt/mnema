package dev.mnema.app.helper;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.text.SimpleDateFormat;
import java.util.Arrays;
import java.util.Date;
import java.util.Locale;

/** Helper log: stdout goes to the log file. */
final class HelperLog {
    private static final SimpleDateFormat TIME = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.ROOT);

    private HelperLog() { }

    static synchronized void line(String text) {
        System.out.println(TIME.format(new Date()) + " " + text);
        System.out.flush();
    }

    static synchronized void error(String text, Throwable error) {
        line(text + ": " + error);
        error.printStackTrace(System.out);
        System.out.flush();
    }

    /** The last maxLines lines of the file, at most maxBytes bytes; a line cut at the front is dropped; no file — null. */
    static byte[] tail(File file, int maxBytes, int maxLines) throws IOException {
        if (!file.isFile()) return null;
        try (RandomAccessFile raf = new RandomAccessFile(file, "r")) {
            long length = raf.length();
            int size = (int) Math.min(length, maxBytes);
            byte[] data = new byte[size];
            raf.seek(length - size);
            raf.readFully(data);
            int start = 0;
            if (length > size) {
                while (start < size && data[start] != '\n') start++;
                start = Math.min(size, start + 1);
            }
            int lines = 0;
            int begin = start;
            for (int i = size - 2; i >= start; i--) {
                if (data[i] == '\n' && ++lines == maxLines) {
                    begin = i + 1;
                    break;
                }
            }
            return Arrays.copyOfRange(data, begin, size);
        }
    }
}
