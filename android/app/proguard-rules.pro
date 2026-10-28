# The helper is started by app_process by class name
-keep class dev.mnema.app.helper.** { *; }

# The debug log stays out of release
-assumenosideeffects class android.util.Log {
    public static int d(...);
    public static int v(...);
}
