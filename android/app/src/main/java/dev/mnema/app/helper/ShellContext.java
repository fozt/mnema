/* Adapted from scrcpy, Copyright (C) 2018-2026 Genymobile, Apache-2.0: see android/THIRD_PARTY.md. */
package dev.mnema.app.helper;

import android.annotation.SuppressLint;
import android.app.Application;
import android.app.Instrumentation;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;

import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;

/** app_process Context with the shell uid for AudioPolicy and AudioRecord. */
final class ShellContext extends ContextWrapper {
    static final int SHELL_UID = 2000;
    static final String PACKAGE = "com.android.shell";

    private final AttributionSource attribution = new AttributionSource.Builder(SHELL_UID).setPackageName(PACKAGE).build();

    private ShellContext(Context base) { super(base); }

    @Override public String getPackageName() { return PACKAGE; }
    @Override public String getOpPackageName() { return PACKAGE; }
    @Override public AttributionSource getAttributionSource() { return attribution; }
    @Override public int getDeviceId() { return 0; }
    @Override public Context getApplicationContext() { return this; }
    @Override public Context createPackageContext(String name, int flags) { return this; }

    // only the hidden ActivityThread gives a shell Context
    @SuppressLint({"PrivateApi", "DiscouragedPrivateApi"})
    static Context create() throws Exception {
        Class<?> threadClass = Class.forName("android.app.ActivityThread");
        Constructor<?> constructor = threadClass.getDeclaredConstructor();
        constructor.setAccessible(true);
        Object thread = constructor.newInstance();
        setField(threadClass, null, "sCurrentActivityThread", thread);
        setField(threadClass, thread, "mSystemThread", true);
        Class<?> internalClass = Class.forName("android.app.ActivityThreadInternal");
        Class<?> controllerClass = Class.forName("android.app.ConfigurationController");
        Constructor<?> controllerConstructor = controllerClass.getDeclaredConstructor(internalClass);
        controllerConstructor.setAccessible(true);
        setField(threadClass, thread, "mConfigurationController", controllerConstructor.newInstance(thread));
        Class<?> bindClass = Class.forName("android.app.ActivityThread$AppBindData");
        Constructor<?> bindConstructor = bindClass.getDeclaredConstructor();
        bindConstructor.setAccessible(true);
        Object bind = bindConstructor.newInstance();
        ApplicationInfo info = new ApplicationInfo();
        info.packageName = PACKAGE;
        info.uid = SHELL_UID;
        info.targetSdkVersion = 37;
        setField(bindClass, bind, "appInfo", info);
        setField(threadClass, thread, "mBoundApplication", bind);
        Method getContext = threadClass.getDeclaredMethod("getSystemContext");
        getContext.setAccessible(true);
        Context context = new ShellContext((Context) getContext.invoke(thread));
        Application application = Instrumentation.newApplication(Application.class, context);
        setField(threadClass, thread, "mInitialApplication", application);
        return context;
    }

    private static void setField(Class<?> type, Object target, String name, Object value) throws Exception {
        Field field = type.getDeclaredField(name);
        field.setAccessible(true);
        field.set(target, value);
    }
}
