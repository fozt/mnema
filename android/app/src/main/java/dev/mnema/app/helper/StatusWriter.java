package dev.mnema.app.helper;

import android.annotation.SuppressLint;

import java.io.File;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.FileAttribute;
import java.nio.file.attribute.PosixFilePermission;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.EnumSet;
import java.util.Locale;
import java.util.Set;

/** status.json of protocol v1 and atomic writes of exchange files. */
final class StatusWriter {
    static final String FILE = "status.json";
    static final int VERSION = 1;
    private static final FileAttribute<Set<PosixFilePermission>> PRIVATE =
            PosixFilePermissions.asFileAttribute(EnumSet.of(PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE));

    private StatusWriter() { }

    /** Helper heartbeat; levels are written only while recording. */
    static final class Status {
        String state = "idle";
        int pid;
        String nonce = "";
        long apkUpdated;
        long heartbeatMs;
        long heartbeatUptimeMs;
        String pong = "";
        String folder = "";
        String lastFolder = "";
        String error = "";
        long startedMs;
        boolean callActive;
        String callMode = CallMode.NONE;
        boolean micOn = true;
        boolean callOn = true;
        boolean paused;
        long pausedMs;
        Double callRms;
        Double micRms;
        Boolean micSilenced;
        Boolean callSound;
        Boolean micSound;
        Long callSilentMs;
        String callSource;
    }

    static void write(File dir, Status status) throws IOException {
        writeAtomic(new File(dir, FILE), json(status).getBytes(StandardCharsets.UTF_8), true);
    }

    static String json(Status s) {
        StringBuilder out = new StringBuilder(512).append('{');
        field(out, "v").append(VERSION);
        quote(field(out, "state"), s.state);
        field(out, "pid").append(s.pid);
        quote(field(out, "nonce"), s.nonce);
        field(out, "apk_updated").append(s.apkUpdated);
        field(out, "heartbeat_ms").append(s.heartbeatMs);
        field(out, "heartbeat_uptime_ms").append(s.heartbeatUptimeMs);
        quote(field(out, "pong"), s.pong);
        quote(field(out, "folder"), s.folder);
        quote(field(out, "last_folder"), s.lastFolder);
        quote(field(out, "error"), s.error);
        field(out, "started_ms").append(s.startedMs);
        field(out, "call_active").append(s.callActive);
        quote(field(out, "call_mode"), s.callMode);
        field(out, "mic_on").append(s.micOn);
        field(out, "call_on").append(s.callOn);
        field(out, "paused").append(s.paused);
        field(out, "paused_ms").append(s.pausedMs);
        if ("recording".equals(s.state)) {
            number(field(out, "call_rms"), s.callRms);
            number(field(out, "mic_rms"), s.micRms);
            field(out, "mic_silenced").append(s.micSilenced == null ? "null" : s.micSilenced.toString());
            field(out, "call_sound").append(s.callSound == null ? "null" : s.callSound.toString());
            field(out, "mic_sound").append(s.micSound == null ? "null" : s.micSound.toString());
            field(out, "call_silent_ms").append(s.callSilentMs == null ? "null" : s.callSilentMs.toString());
            if (s.callSource == null) field(out, "call_source").append("null");
            else quote(field(out, "call_source"), s.callSource);
        }
        return out.append('}').toString();
    }

    /** tmp + rename without fsync; shared — o+r, another uid reads the file. */
    @SuppressLint("SetWorldReadable")
    static void writeAtomic(File target, byte[] data, boolean shared) throws IOException {
        Path temporary = new File(target.getParentFile(), target.getName() + ".tmp").toPath();
        // a symlink in place of tmp is removed: CREATE_NEW does not write through it
        Files.deleteIfExists(temporary);
        try {
            try (FileChannel channel = FileChannel.open(temporary,
                    EnumSet.of(StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE), PRIVATE)) {
                ByteBuffer buffer = ByteBuffer.wrap(data);
                while (buffer.hasRemaining()) channel.write(buffer);
            }
            if (shared && !temporary.toFile().setReadable(true, false)) throw new IOException("Cannot chmod " + temporary);
            Files.move(temporary, target.toPath(), StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException | RuntimeException error) {
            try { Files.deleteIfExists(temporary); } catch (IOException suppressed) { error.addSuppressed(suppressed); }
            throw error;
        }
    }

    private static StringBuilder field(StringBuilder out, String name) {
        if (out.length() > 1) out.append(',');
        return out.append('"').append(name).append("\":");
    }

    private static void number(StringBuilder out, Double value) {
        out.append(value == null || value.isNaN() || value.isInfinite() ? "null" : value.toString());
    }

    private static void quote(StringBuilder out, String value) {
        if (value == null) value = "";
        out.append('"');
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (c == '"' || c == '\\') out.append('\\').append(c);
            else if (c < 0x20 || c == '\u2028' || c == '\u2029') out.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
            else out.append(c);
        }
        out.append('"');
    }
}
