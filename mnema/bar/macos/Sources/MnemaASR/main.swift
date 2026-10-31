// mnema-asr: recognition on CoreML through FluidAudio for Mnema on the Mac. Python (the app and
// the command) starts it, sends JSON requests on stdin and reads replies on stdout
// (MnemaASRProtocol). Requests run one after another; at EOF of stdin the helper finishes what it
// was sent and exits. A parent that died is noticed on the next reply: the write gets SIGPIPE.
import Foundation
import MnemaASRProtocol

if CommandLine.arguments.dropFirst().first == "--version" {
    print("mnema-asr \(ASRWire.version)")
    exit(0)
}

let output = Output.takeStdout()
// Word times as on the server: a token starts at its own frame (FluidAudio shifts by one).
setenv("TDT_EMISSION_DELAY_FRAMES", "0", 0)
let engine = Engine(store: .standard(), send: { output.send($0) })

let lines = AsyncStream<String> { stream in
    let reader = Thread {
        while let line = readLine(strippingNewline: true) {
            stream.yield(line)
        }
        stream.finish()
    }
    reader.name = "mnema-asr-stdin"
    reader.start()
}

for await text in lines where !text.trimmingCharacters(in: .whitespaces).isEmpty {
    do {
        let request = try ASRWire.decode(line: Data(text.utf8))
        output.send(try await engine.run(request))
    } catch let failure as ASRFailure {
        Log.error("\(failure.code.rawValue): \(failure.detail)")
        output.send(.error(failure))
    } catch {
        Log.error("failed: \(error)")
        output.send(.error(ASRFailure(.failed, "\(error)")))
    }
}
exit(0)
