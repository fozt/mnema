package dev.mnema.app.helper;

import android.media.AudioManager;

/** Audio mode → call: cellular, messenger, incoming or none; the values go to status.json and the report. */
final class CallMode {
    static final String NONE = "none";
    static final String RINGING = "ringing";
    static final String CELLULAR = "cellular";
    static final String MESSENGER = "messenger";

    private CallMode() { }

    // javac inlines these constants as numbers: the device code does not have them
    static String of(int mode) {
        switch (mode) {
            case AudioManager.MODE_RINGTONE:
                return RINGING;
            case AudioManager.MODE_IN_CALL:
            case AudioManager.MODE_CALL_SCREENING:
            case AudioManager.MODE_CALL_REDIRECT:
                return CELLULAR;
            case AudioManager.MODE_IN_COMMUNICATION:
            case AudioManager.MODE_COMMUNICATION_REDIRECT:
                return MESSENGER;
            default:
                return NONE;
        }
    }

    /** A conversation is going on: cellular or messenger. */
    static boolean call(String kind) { return CELLULAR.equals(kind) || MESSENGER.equals(kind); }

    /** A call or an incoming call: a recording started in it stops after it. */
    static boolean engaged(String kind) { return call(kind) || RINGING.equals(kind); }
}
