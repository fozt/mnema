import Foundation

/// The Python venv runs inside the Mnema.app process on its own thread: macOS grants microphone
/// and system audio permissions to the app process. A child python process did not get them and
/// recorded system audio as silence.
final class EmbeddedPython {
    private typealias InitializeEx = @convention(c) (Int32) -> Void
    private typealias RunString = @convention(c) (UnsafePointer<CChar>) -> Int32
    private typealias SaveThread = @convention(c) () -> OpaquePointer?
    private typealias RestoreThread = @convention(c) (OpaquePointer?) -> Void

    private let initialize: InitializeEx
    private let runString: RunString
    private let saveThread: SaveThread
    private let restoreThread: RestoreThread
    private var threadState: OpaquePointer?
    private var initialized = false
    private let requests = DispatchSemaphore(value: 0)
    private var thread: Thread?
    private var onExit: (Int32) -> Void = { _ in }

    init(library: String) throws {
        guard let handle = dlopen(library, RTLD_NOW | RTLD_GLOBAL) else {
            throw EmbeddedPythonError.load(String(cString: dlerror()))
        }
        func symbol<T>(_ name: String, as _: T.Type) throws -> T {
            guard let pointer = dlsym(handle, name) else { throw EmbeddedPythonError.symbol(name) }
            return unsafeBitCast(pointer, to: T.self)
        }
        // An app started from Finder has no locale and Py_InitializeEx ignores PYTHONUTF8: text would be ASCII
        dlsym(handle, "Py_UTF8Mode")?.assumingMemoryBound(to: Int32.self).pointee = 1
        initialize = try symbol("Py_InitializeEx", as: InitializeEx.self)
        runString = try symbol("PyRun_SimpleString", as: RunString.self)
        saveThread = try symbol("PyEval_SaveThread", as: SaveThread.self)
        restoreThread = try symbol("PyEval_RestoreThread", as: RestoreThread.self)
    }

    /// Starts the engine (`mnema-bar --headless`). `done` runs on the engine thread when it exits:
    /// 0 on a normal exit, otherwise an exception (traceback goes to stderr, i.e. the log). The
    /// interpreter lives until the app quits: a restart is a new run in the same interpreter.
    func run(done: @escaping (Int32) -> Void) {
        onExit = done
        if thread == nil {
            let t = Thread { [unowned self] in self.loop() }
            t.stackSize = 16 << 20  // the default thread stack (512 KB) is too small for Python
            t.name = "mnema-engine"
            thread = t
            t.start()
        }
        requests.signal()
    }

    private func loop() {
        while true {
            requests.wait()
            if initialized {
                restoreThread(threadState)
            } else {
                initialize(0)  // no signal handlers: the engine installs its own
                initialized = true
            }
            let code = Self.script.withCString { runString($0) }
            threadState = saveThread()  // release the GIL until the engine runs again
            onExit(code)
        }
    }

    private static let script = """
        import runpy, sys
        sys.argv = ["mnema-bar", "--headless"]
        try:
            runpy.run_module("mnema.bar", run_name="__main__", alter_sys=True)
        except SystemExit:
            pass
        """
}

enum EmbeddedPythonError: Error {
    case load(String)
    case symbol(String)
}
