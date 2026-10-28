package dev.mnema.app.helper;

import java.io.Closeable;
import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;

/** Mono PCM16 WAV with a 44-byte header; header and fsync every second. */
final class WavWriter implements Closeable {
    private final RandomAccessFile file;
    private final int sampleRate;
    private byte[] bytes = new byte[0];
    private long dataBytes;
    private long checkpointBytes;
    private boolean closed;

    WavWriter(File path, int sampleRate) throws IOException {
        if (sampleRate <= 0) throw new IllegalArgumentException("sampleRate");
        this.sampleRate = sampleRate;
        file = new RandomAccessFile(path, "rw");
        try {
            file.setLength(0);
            header(0);
            file.getFD().sync();
        } catch (IOException error) {
            try { file.close(); } catch (IOException ignored) { }
            throw error;
        }
    }

    synchronized void write(short[] samples, int count) throws IOException {
        if (closed) throw new IOException("WAV is closed");
        if (count < 0 || count > samples.length) throw new IllegalArgumentException("count");
        int length = Math.multiplyExact(count, 2);
        if (dataBytes + length > 0xffffffffL - 36) throw new IOException("WAV exceeds RIFF size");
        if (bytes.length < length) bytes = new byte[length];
        for (int i = 0; i < count; i++) {
            bytes[2 * i] = (byte) samples[i];
            bytes[2 * i + 1] = (byte) (samples[i] >> 8);
        }
        file.seek(44 + dataBytes);
        file.write(bytes, 0, length);
        dataBytes += length;
        // SIGKILL leaves no chance to call close
        if (dataBytes - checkpointBytes >= sampleRate * 2L) {
            header(dataBytes);
            file.getFD().sync();
            checkpointBytes = dataBytes;
        }
    }

    private void le16(int value) throws IOException {
        file.write(value & 255);
        file.write((value >>> 8) & 255);
    }

    private void le32(long value) throws IOException {
        le16((int) value);
        le16((int) (value >>> 16));
    }

    private void header(long length) throws IOException {
        file.seek(0);
        file.writeBytes("RIFF");
        le32(36 + length);
        file.writeBytes("WAVEfmt ");
        le32(16);
        le16(1);
        le16(1);
        le32(sampleRate);
        le32(sampleRate * 2L);
        le16(2);
        le16(16);
        file.writeBytes("data");
        le32(length);
        file.seek(44 + length);
    }

    @Override public synchronized void close() throws IOException {
        if (closed) return;
        closed = true;
        IOException failure = null;
        try {
            // the tail after a write failure is dropped
            file.setLength(44 + dataBytes);
            header(dataBytes);
            file.getFD().sync();
        } catch (IOException error) {
            failure = error;
        } finally {
            try { file.close(); } catch (IOException error) {
                if (failure == null) failure = error;
                else failure.addSuppressed(error);
            }
        }
        if (failure != null) throw failure;
    }
}
