package dev.mnema.app.processing

import dev.mnema.app.model.Lang
import dev.mnema.app.recordings.ProcessingState
import dev.mnema.app.recordings.nameProcessing
import dev.mnema.app.recordings.outcomeKey
import dev.mnema.app.recordings.recordingId
import dev.mnema.app.server.RunnerClient
import dev.mnema.app.server.parseConnection
import dev.mnema.app.settings.NodeEntry
import dev.mnema.app.settings.Settings
import dev.mnema.app.settings.nodeAddress
import dev.mnema.app.settings.SummaryLang
import dev.mnema.app.settings.settingsFor
import dev.mnema.app.sync.resource
import dev.mnema.app.transcript.Meta
import dev.mnema.app.transcript.WavFixture
import java.io.File
import java.nio.file.Files
import java.util.TimeZone
import java.util.concurrent.CopyOnWriteArrayList
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import mockwebserver3.SocketEffect
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class PipelineTest {
    @get:Rule val tmp = TemporaryFolder()
    private val server = MockWebServer()
    private val http = OkHttpClient.Builder().retryOnConnectionFailure(false).build()
    private val calls = CopyOnWriteArrayList<String>()
    private val submitted = CopyOnWriteArrayList<JsonObject>()
    private val uploaded = CopyOnWriteArrayList<String>()
    private val persisted = CopyOnWriteArrayList<ProcessState>()
    private val progress = CopyOnWriteArrayList<ProcessingState>()
    // null — a normal answer: the new job id, done with a result
    private val submitReplies = ArrayDeque<MockResponse?>()
    private val pollReplies = ArrayDeque<MockResponse?>()
    private val deleteReplies = ArrayDeque<MockResponse>()
    private var recording: File? = null
    private var count = 0
    private var down = false
    private var encoded = 0
    private var summaryOnly = false
    private var doneResult: String? = null
    // how the node names itself in capabilities; null — it does not answer them
    private var nodeName: String? = null
    // the card while the job is being submitted
    private var whileSubmitting: () -> Unit = {}
    private val now = 1_800_000_000_000L
    private val cache get() = File(tmp.root, "cache")
    private val connection get() = server.url("/").toString().trimEnd('/') + "#token=test-token"
    private fun settings() = settingsFor(connection, Lang.RU).copy(summaryLang = SummaryLang.EN)
    private val nodeDown get() = Problems.nodeDown(settings().activeEntry()!!.name)
    private fun client() = RunnerClient(parseConnection(connection), http)
    private val encoder = object : AudioEncoder {
        override fun encode(wav: File, out: File) { encoded++; wav.copyTo(out, overwrite = true) }
    }

    @Before fun setup() {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.url.encodedPath
                calls += "${request.method} $path"
                if (down) return MockResponse(503, body = "{}")
                return when {
                    request.method == "POST" && path == "/api/uploads" -> {
                        val body = Json.parseToJsonElement(request.body!!.utf8()).jsonObject
                        uploaded += body["name"]!!.jsonPrimitive.content
                        MockResponse(201, body = """{"id":"u${uploaded.size}","offset":0}""")
                    }
                    request.method == "PUT" && path.startsWith("/api/uploads/") ->
                        MockResponse(200, body = """{"offset":${request.body!!.size},"path":"uploads/${path.substringAfterLast('/')}/file"}""")
                    request.method == "POST" && path == "/api/jobs" -> {
                        submitted += Json.parseToJsonElement(request.body!!.utf8()).jsonObject
                        recording?.let { ProcessStateFile.read(it) }?.let(persisted::add)
                        whileSubmitting()
                        submitReplies.removeFirstOrNull() ?: MockResponse(200, body = """{"id":"j${++count}"}""")
                    }
                    request.method == "GET" && path.startsWith("/api/jobs/") ->
                        pollReplies.removeFirstOrNull() ?: done(path.substringAfterLast('/'))
                    request.method == "DELETE" -> if (deleteReplies.isEmpty()) MockResponse(204) else deleteReplies.removeFirst()
                    request.method == "POST" && path.endsWith("/cancel") -> MockResponse(200, body = "{}")
                    request.method == "GET" && path == "/api/capabilities" && nodeName != null ->
                        MockResponse(200, body = """{"ok":true,"llm":true,"name":"$nodeName"}""")
                    else -> MockResponse(404, body = "{}")
                }
            }
        }
        server.start()
    }

    @After fun teardown() { server.close(); http.dispatcher.executorService.shutdown(); http.connectionPool.evictAll() }

    private fun done(id: String): MockResponse {
        doneResult?.let { return MockResponse(200, body = """{"id":"$id","status":"done","result":$it}""") }
        val files = if (summaryOnly) """{"summary.md":"Итог.\n\n- x\n"}""" else """{"transcript.txt":"[0:12] mic: x\n","summary.md":null}"""
        return MockResponse(200, body = """{"id":"$id","status":"done","result":{"files":$files,"folder":"2026-10-22_Дейли","run":{"status":"ok","settings":{"node":"Узел"},"remarks":[]}}}""")
    }

    private fun recording(system: Boolean = true, silentMic: Boolean = false, name: String = "2026-10-22_16-01"): File = tmp.newFolder(name).also { dir ->
        recording = dir
        WavFixture.write(File(dir, "mic.wav"), WavFixture.samples(2.0, loud = if (silentMic) emptyList() else listOf(0.2 to 1.8)))
        if (system) WavFixture.write(File(dir, "system.wav"), WavFixture.samples(2.0, loud = listOf(0.2 to 1.8)))
        helperReport(dir, "stopped", "stop")
        Meta.ensureRecording(dir, "record-id") { Meta.isoNow(1792674067000, TimeZone.getTimeZone("GMT+03:00")) }
        ProcessStateFile.write(dir, ProcessState(id = "record-id", since = outcomeKey(Meta.read(dir))))
    }

    @Test fun historyOfANewerVersionFailsTheWorkInsteadOfStartingItAgainAndAgain() {
        val dir = recording()
        val meta = Json.parseToJsonElement(File(dir, Meta.FILE).readText()).jsonObject
        File(dir, Meta.FILE).writeText(JsonObject(meta + ("version" to JsonPrimitive(2))).toString())

        val (result, _) = step(dir)

        assertTrue(result is StepResult.Failed)
        val work = ProcessStateFile.current(dir)!!
        assertEquals(ProcessStage.FAILED, work.stage)
        assertEquals((result as StepResult.Failed).message, work.problem)
        assertTrue(WorkScheduler.orphaned(tmp.root) { false }.isEmpty())
        assertTrue(calls.isEmpty())
    }

    /** A report with the fields the helper's Capture.finish and writeReport write. */
    private fun helperReport(dir: File, status: String, stopReason: String, vararg errors: String, reason: String? = null) {
        val list = errors.joinToString(", ", "[", "]") { "\"$it\"" }
        val errorReason = reason?.let { "\n  \"error_reason\": \"$it\"," }.orEmpty()
        File(dir, ".mnema-helper-report.json").writeText("""{
          |  "schema": 1,
          |  "uid": 2000,
          |  "model": "Pixel",
          |  "api": 37,
          |  "sample_rate": 16000,
          |  "started_at_unix_ms": 1792674067000,
          |  "route": "LOOP_BACK_RENDER",
          |  "status": "$status",
          |  "stop_reason": "$stopReason",
          |  "total": {"samples": 32000, "audio_seconds": 2, "all_zero": false},
          |  "finished_at_unix_ms": 1792674069000,
          |  "elapsed_ms": 2100,
          |  "capture_elapsed_ms": 2000,
          |  "errors": $list,$errorReason
          |  "warnings": []
          |}
          |""".trimMargin())
    }

    // the step is running out: it asks about the node's running job once
    private fun step(dir: File, runner: RunnerClient? = client(), settings: Settings = settings()): Pair<StepResult, File> = runBlocking {
        Pipeline(settings, runner, encoder, cache, { now }, { null }).step(dir, now + 1_000) { progress += it }
    }

    private fun retry(dir: File) {
        val before = ProcessStateFile.read(dir)!!
        ProcessStateFile.write(dir, queued(before, before.summaryOnly, id = before.id, since = outcomeKey(Meta.read(dir))))
    }

    // a new processing from the window or a button, as WorkScheduler queues it
    private fun again(dir: File, summaryOnly: Boolean = false, options: ReprocessOptions? = null) =
        ProcessStateFile.write(dir, queued(ProcessStateFile.read(dir), summaryOnly, options, recordingId(dir)!!, outcomeKey(Meta.read(dir))))

    @Test fun twoTracksUseOneNodeJobAndApplyBeforeDeletingIt() {
        val dir = recording()
        File(dir, "summary.md").writeText("старое")
        val (result, folder) = step(dir)
        assertEquals(StepResult.Done, result)
        assertEquals(listOf("mic.m4a", "system.m4a"), uploaded)
        assertEquals(1, submitted.size)
        val payload = submitted.single()
        assertEquals("recording", payload["op"]!!.jsonPrimitive.content)
        assertEquals(setOf("mic", "system"), payload["tracks"]!!.jsonObject.keys)
        val params = payload["params"]!!.jsonObject
        assertEquals("2026-10-22T16:01:07+03:00", params["started"]!!.jsonPrimitive.content)
        assertEquals("2026-10-22_16-01", params["name"]!!.jsonPrimitive.content)
        assertEquals("auto", params["asr"]!!.jsonPrimitive.content)
        assertEquals("ru", params["lang"]!!.jsonPrimitive.content)
        assertEquals("en", params["summary_lang"]!!.jsonPrimitive.content)
        assertEquals(JsonNull, params["recording_error"])
        assertEquals(JsonPrimitive(true), params["partial"])
        assertEquals(payload["key"]!!.jsonPrimitive.content, persisted.single().key)
        assertEquals(dir.name, persisted.single().sentName)
        assertEquals("[0:12] mic: x\n", File(folder, "transcript.txt").readText())
        assertFalse(File(folder, "summary.md").exists())
        assertEquals("j1", Meta.lastRun(folder)!!["job"]!!.jsonPrimitive.content)
        assertEquals(ProcessStage.DONE, ProcessStateFile.read(folder)!!.stage)
        assertNull(ProcessStateFile.read(folder)!!.problem)
        assertEquals("DELETE /api/jobs/j1", calls.last())
    }

    @Test fun helperErrorReportSendsItsReasonAsRecordingError() {
        val dir = recording()
        helperReport(dir, "error", "error", "java.io.IOException: Mic AudioRecord.read error -3", "java.io.IOException: close failed",
            reason = "system audio was lost")
        assertEquals(StepResult.Done, step(dir).first)
        assertEquals("пропал системный звук", submitted.single()["params"]!!.jsonObject["recording_error"]!!.jsonPrimitive.content)
        val other = tmp.newFolder("report")
        helperReport(other, "error", "error", reason = "x".repeat(600))
        assertEquals(500, recordingError(other)!!.length)
        helperReport(other, "recording", "stop", "java.io.IOException: ignored")
        assertNull(recordingError(other))
        File(other, ".mnema-helper-report.json").delete()
        assertNull(recordingError(other))
    }

    @Test fun lostSubmitReplyUsesThePersistedKeyAndCannotBecomeAnEndlessRetry() {
        // one lost answer: a technical retry with the same key finishes it
        val once = recording()
        submitReplies += MockResponse.Builder().onResponseStart(SocketEffect.CloseSocket()).build()
        assertEquals(StepResult.Done, step(once).first)
        assertEquals(2, submitted.size)
        assertEquals(submitted[0]["key"], submitted[1]["key"])

        // two in a row: an error and a manual retry with the same key
        submitted.clear()
        uploaded.clear()
        val dir = recording(name = "2026-10-22_17-30")
        repeat(2) { submitReplies += MockResponse.Builder().onResponseStart(SocketEffect.CloseSocket()).build() }
        assertEquals(StepResult.Failed(nodeDown), step(dir).first)
        assertEquals(2, submitted.size)
        assertEquals(submitted[0]["key"], submitted[1]["key"])
        val key = ProcessStateFile.read(dir)!!.key
        retry(dir)
        val (_, folder) = step(dir)
        assertEquals(key, submitted.last()["key"]!!.jsonPrimitive.content)
        assertEquals(2, uploaded.size)
        assertEquals(ProcessStage.DONE, ProcessStateFile.read(folder)!!.stage)
    }

    @Test fun renamedNodeIsCalledByItsNewNameWhenItStopsAnswering() {
        val dir = recording()
        nodeName = "Ноутбук на работе"
        val names = mutableListOf<String>()
        repeat(2) { submitReplies += MockResponse.Builder().onResponseStart(SocketEffect.CloseSocket()).build() }

        val (result, _) = step(dir, RunnerClient(parseConnection(connection), http) { names += it })

        assertEquals(StepResult.Failed(Problems.nodeDown("Ноутбук на работе")), result)
        assertEquals(listOf("Ноутбук на работе"), names)
    }

    @Test fun unavailableNodeFailsAndDoesNotResumeUntilManualRetry() {
        val dir = recording()
        down = true
        assertEquals(StepResult.Failed(nodeDown), step(dir).first)
        assertEquals(ProcessStage.FAILED, ProcessStateFile.read(dir)!!.stage)
        val before = calls.size
        down = false
        assertEquals(StepResult.Failed(nodeDown), step(dir).first)
        assertEquals(before, calls.size)
        retry(dir)
        val (_, folder) = step(dir)
        assertEquals(ProcessStage.DONE, ProcessStateFile.read(folder)!!.stage)
        assertNull(ProcessStateFile.read(folder)!!.problem)
    }

    @Test fun unchangedRetryOfAChosenRunPollsItsJobWithoutUploadingAgain() {
        val dir = recording()
        val chosen = ReprocessOptions(lang = Lang.EN, tracks = setOf("mic"))
        again(dir, options = chosen)
        pollReplies += MockResponse(503, body = "{}")
        assertEquals(StepResult.Failed(nodeDown), step(dir).first)
        assertEquals("j1", ProcessStateFile.read(dir)!!.job)

        retry(dir)
        val (result, folder) = step(dir)

        assertEquals(StepResult.Done, result)
        assertEquals(listOf("mic.m4a"), uploaded)
        assertEquals(1, submitted.size)
        assertEquals("en", submitted.single()["params"]!!.jsonObject["lang"]!!.jsonPrimitive.content)
        assertEquals(2, calls.count { it == "GET /api/jobs/j1" })
        assertEquals(chosen, ProcessStateFile.read(folder)!!.options)
    }

    @Test fun unavailableNodeIsCalledByItsNameAndOnlyThatFailureOffersAnotherNode() {
        val dir = recording()
        val named = Settings(nodes = listOf(NodeEntry(connection, "Mac mini")), activeNode = nodeAddress(connection))
        down = true
        assertEquals(StepResult.Failed("Mac mini недоступен"), step(dir, settings = named).first)
        assertTrue(ProcessStateFile.read(dir)!!.unreachable)
        retry(dir)
        assertFalse(ProcessStateFile.read(dir)!!.unreachable)
        down = false
        submitReplies += MockResponse(401, body = "{}")
        assertEquals(StepResult.Failed(Problems.TOKEN), step(dir, settings = named).first)
        assertFalse(ProcessStateFile.read(dir)!!.unreachable)
    }

    @Test fun noNodeDoesNotPrepareAudioOrAppendHistory() {
        val dir = recording()
        assertEquals(StepResult.AudioOnly, step(dir, runner = null).first)
        assertEquals(0, encoded)
        assertTrue(calls.isEmpty())
        assertEquals(Problems.NO_NODE, ProcessStateFile.read(dir)!!.problem)
        assertTrue(Meta.read(dir)!!["runs"]!!.jsonArray.isEmpty())
    }

    @Test fun silentTracksGoToTheServerAndOnlyUnchosenOnesStay() {
        // the server decides on silence, as on desktop: a silent track goes with the others
        val dir = recording(silentMic = true)
        val (_, folder) = step(dir)
        assertEquals(listOf("mic.m4a", "system.m4a"), uploaded)
        again(folder, options = ReprocessOptions(tracks = setOf("system"), asr = "groq/whisper"))
        step(folder)
        val request = submitted.last()
        assertEquals(setOf("system"), request["tracks"]!!.jsonObject.keys)
        assertEquals("groq/whisper", request["params"]!!.jsonObject["asr"]!!.jsonPrimitive.content)
    }

    // track summaries as the helper's Capture writes them: muted_samples — samples of a muted source
    private fun mutedReport(dir: File, systemMuted: Long, micMuted: Long) = File(dir, ".mnema-helper-report.json").writeText(
        """{"status":"stopped","total":{"samples":32000,"muted_samples":$systemMuted},"mic_total":{"samples":32000,"muted_samples":$micMuted}}""")

    @Test fun sourceOffForTheWholeRecordingIsSkippedLikeAnUnchosenOne() {
        val dir = recording(system = false)
        WavFixture.write(File(dir, "system.wav"), WavFixture.samples(2.0, loud = emptyList()))
        mutedReport(dir, systemMuted = 32000, micMuted = 0)
        assertEquals(setOf("system"), mutedTracks(dir))
        assertEquals(StepResult.Done, step(dir).first)
        assertEquals(listOf("mic.m4a"), uploaded)
        assertEquals(setOf("mic"), submitted.single()["tracks"]!!.jsonObject.keys)
    }

    // recording seconds as the helper's Capture.addSeconds writes them
    private fun secondsReport(dir: File, seconds: JsonElement, systemMuted: Long = 0) = File(dir, ".mnema-helper-report.json").writeText(
        """{"status":"stopped","total":{"samples":32000,"muted_samples":$systemMuted},"mic_total":{"samples":32000,"muted_samples":0},"seconds":$seconds}""")

    @Test fun whyTheOtherSideWasSilentGoesFromTheHelperReportToTheNode() {
        val cases = Json.parseToJsonElement(resource("silent-reason-v1.json")).jsonObject.getValue("cases").jsonArray.map { it.jsonObject }
        cases.forEachIndexed { index, case ->
            val reason = case.getValue("reason").jsonPrimitive.contentOrNull
            val dir = recording(system = false, name = "2026-10-22_16-0$index")
            WavFixture.write(File(dir, "system.wav"), WavFixture.samples(2.0, loud = if (reason == null) listOf(0.2 to 1.8) else emptyList()))
            secondsReport(dir, case.getValue("seconds"))
            assertEquals(case.getValue("name").jsonPrimitive.content, reason, otherSideSilence(dir))

            assertEquals(StepResult.Done, step(dir).first)
            val params = submitted.last()["params"]!!.jsonObject
            if (reason == null) {
                assertTrue("no reason", params["silent_reason"]?.jsonObject.isNullOrEmpty())
            } else {
                // the server decides on silence: a silent track goes to it, the reason only refines its remark
                assertTrue("system" in submitted.last()["tracks"]!!.jsonObject)
                assertEquals(buildJsonObject { put("system", reason) }, params["silent_reason"])
            }
        }

        val muted = recording(system = false, name = "2026-10-22_17-00")
        WavFixture.write(File(muted, "system.wav"), WavFixture.samples(2.0))
        secondsReport(muted, cases.first().getValue("seconds"), systemMuted = 32000)
        step(muted)
        assertTrue("a muted source is not the other side's silence",
            submitted.last()["params"]!!.jsonObject["silent_reason"]?.jsonObject.isNullOrEmpty())
    }

    @Test fun sourceOffForPartOfTheRecordingOrChosenInTheSheetIsRecognized() {
        val part = recording()
        mutedReport(part, systemMuted = 16000, micMuted = 31999)
        assertEquals(emptySet<String>(), mutedTracks(part))
        assertEquals(StepResult.Done, step(part).first)
        assertEquals(listOf("mic.m4a", "system.m4a"), uploaded)

        val chosen = tmp.newFolder("chosen")
        WavFixture.write(File(chosen, "mic.wav"), WavFixture.samples(2.0, loud = listOf(0.2 to 1.8)))
        WavFixture.write(File(chosen, "system.wav"), WavFixture.samples(2.0, loud = listOf(0.2 to 1.8)))
        mutedReport(chosen, systemMuted = 32000, micMuted = 0)
        Meta.ensureRecording(chosen, "chosen-id") { Meta.isoNow(1792674067000, TimeZone.getTimeZone("GMT+03:00")) }
        ProcessStateFile.write(chosen, ProcessState(id = "chosen-id", options = ReprocessOptions(tracks = setOf("mic", "system"))))
        uploaded.clear()
        val (result, folder) = step(chosen)
        assertEquals(StepResult.Done, result)
        assertEquals(listOf("mic.m4a", "system.m4a"), uploaded)
        File(folder, ".mnema-helper-report.json").writeText("{")
        assertEquals(emptySet<String>(), mutedTracks(folder))
    }

    @Test fun previousTranscriptAsksTheNodeForFailureInsteadOfHalfARecording() {
        val dir = recording()
        File(dir, "transcript.txt").writeText(" \n")
        val (_, folder) = step(dir)
        assertEquals(JsonPrimitive(true), submitted.single()["params"]!!.jsonObject["partial"])
        again(folder)
        step(folder)
        assertEquals(JsonPrimitive(false), submitted.last()["params"]!!.jsonObject["partial"])
    }

    @Test fun summaryUsesTranscriptAndThePreviousRunWithoutEncodingAudio() {
        val dir = recording()
        File(dir, "transcript.txt").writeText("[0:12] mic: x\n")
        val past = buildJsonObject { put("job", "past"); put("custom", "keep") }
        Meta.appendRun(dir, past)
        ProcessStateFile.write(dir, ProcessState("record-id", summaryOnly = true))
        summaryOnly = true
        assertEquals(StepResult.Done, step(dir).first)
        assertEquals(0, encoded)
        assertEquals(listOf("transcript.txt"), uploaded)
        assertEquals("recording_summary", submitted.single()["op"]!!.jsonPrimitive.content)
        assertEquals(past, submitted.single()["params"]!!.jsonObject["past"])
    }

    @Test fun failedFileApplyKeepsTheNodeResultAndRetryAfterRenameCompletesOnce() {
        val dir = recording()
        val blocker = File(dir, "summary.md").apply { mkdir() }
        assertTrue(step(dir).first is StepResult.Again)
        assertFalse(calls.any { it.startsWith("DELETE") })
        blocker.delete()
        val renamed = File(dir.parentFile, "Моё имя")
        Files.move(dir.toPath(), renamed.toPath())
        val (_, folder) = step(renamed)
        assertEquals(renamed, folder)
        assertEquals(1, submitted.size)
        assertEquals(1, Meta.read(folder)!!["runs"]!!.jsonArray.size)
    }

    @Test fun folderAndroidRefusesToRenameFinishesTheWorkInTheSameFolderWithARemark() {
        val dir = recording()
        val refused = { _: File, _: File -> throw java.nio.file.FileSystemException("Operation not permitted") }
        val pipeline = Pipeline(settings(), client(), encoder, cache, { now }, { null }, moveFolder = refused)

        val (result, folder) = runBlocking { pipeline.step(dir, now + 1_000) { progress += it } }

        assertEquals(StepResult.Done, result)
        assertEquals(dir, folder)
        assertEquals("2026-10-22_16-01", folder.name)
        assertEquals("[0:12] mic: x\n", File(folder, "transcript.txt").readText())
        assertEquals(ProcessStage.DONE, ProcessStateFile.read(folder)!!.stage)
        assertNull(ProcessStateFile.read(folder)!!.problem)
        assertTrue(Meta.lastRun(folder)!!.getValue("remarks").jsonArray.single().jsonPrimitive.content
            .startsWith("Название «Дейли» не применено"))
        assertEquals("DELETE /api/jobs/j1", calls.last())
    }

    @Test fun appliedResultSurvivesUnavailableDeleteWithoutOverwritingUserEdits() {
        val dir = recording()
        deleteReplies += MockResponse(503, body = "{}")
        val (failed, folder) = step(dir)
        assertEquals(StepResult.Failed(nodeDown), failed)
        File(folder, "transcript.txt").writeText("правка")
        retry(folder)
        assertEquals(StepResult.Done, step(folder).first)
        assertEquals("правка", File(folder, "transcript.txt").readText())
        assertEquals(1, submitted.size)
        assertEquals(1, calls.count { it.startsWith("GET /api/jobs") })
        assertEquals(1, Meta.read(folder)!!["runs"]!!.jsonArray.size)
    }

    @Test fun nodeProgressStaysShownWhileTheStepWaitsForTheJob() {
        val dir = recording()
        pollReplies += MockResponse(200, body = """{"id":"j1","status":"pending","message":"В очереди"}""")
        pollReplies += MockResponse(200, body = """{"id":"j1","status":"running","progress":0.25,"message":"Распознаю"}""")
        pollReplies += MockResponse(200, body = """{"id":"j1","status":"running","progress":0.7,"message":"Распознаю"}""")
        var clock = now
        val waits = mutableListOf<Long>()
        val pipeline = Pipeline(settings(), client(), encoder, cache, { clock }, { null }, wait = { ms ->
            waits += ms
            clock += ms
        })

        val (result, _) = runBlocking { pipeline.step(dir, now + 60_000) { progress += it } }

        assertEquals(StepResult.Done, result)
        // waited in the server queue — "Queued", then the recognition share
        assertEquals(
            listOf(ProcessingState.Queued, ProcessingState.Recognizing(0.25, "Распознаю"),
                ProcessingState.Recognizing(0.7, "Распознаю")),
            progress.filter { it == ProcessingState.Queued || it is ProcessingState.Recognizing },
        )
        assertEquals(listOf(5_000L, 5_000L, 5_000L), waits)
        assertEquals(4, calls.count { it == "GET /api/jobs/j1" })
    }

    @Test fun stepEndsBeforeItsDeadlineAndTheNextOneContinuesPolling() {
        val dir = recording()
        repeat(4) { pollReplies += MockResponse(200, body = """{"id":"j1","status":"running","progress":0.5}""") }
        var clock = now
        val pipeline = Pipeline(settings(), client(), encoder, cache, { clock }, { null }, wait = { clock += it })

        val (result, _) = runBlocking { pipeline.step(dir, now + 12_000) { progress += it } }

        assertEquals(StepResult.Again(5_000), result)
        // polls at 0, 5 and 10 s; the next one would not make it before the end of the step
        assertEquals(3, calls.count { it == "GET /api/jobs/j1" })
        assertEquals(now + 10_000, clock)
    }

    @Test fun explicitAutoNameAndTitleUseTheRealFolderName() {
        val dir = recording()
        again(dir, options = ReprocessOptions(autoName = true, title = "Моё название"))
        step(dir)
        val params = submitted.single()["params"]!!.jsonObject
        assertEquals(JsonPrimitive(true), params["retitle"])
        assertEquals(JsonPrimitive("Моё название"), params["user_title"])
        assertEquals(JsonPrimitive(dir.name), params["name"])
    }

    @Test fun aCompletedJobWithFailedRunIsNotReportedAsSuccessAndCanBeRetried() {
        val dir = recording()
        File(dir, "transcript.txt").writeText("прежний текст")
        pollReplies += failedRun()
        assertEquals(StepResult.Failed("Нет места на узле"), step(dir).first)
        assertEquals("прежний текст", File(dir, "transcript.txt").readText())
        assertEquals("failed", Meta.lastRun(dir)!!["status"]!!.jsonPrimitive.content)
        assertEquals("DELETE /api/jobs/j1", calls.last())
        retry(dir)
        assertEquals(StepResult.Done, step(dir).first)
        assertNotEquals(submitted[0]["key"], submitted[1]["key"])
    }

    @Test fun failedRunAfterLostForgetUsesItsOwnHistoryAndKeepsEditedText() {
        val dir = recording()
        pollReplies += failedRun()
        deleteReplies += MockResponse(503, body = "{}")
        assertEquals(StepResult.Failed(nodeDown), step(dir).first)
        File(dir, "transcript.txt").writeText("ручной текст")
        retry(dir)
        assertEquals(StepResult.Failed("Нет места на узле"), step(dir).first)
        assertEquals("ручной текст", File(dir, "transcript.txt").readText())
        assertEquals(1, submitted.size)
        assertEquals(1, calls.count { it.startsWith("GET /api/jobs") })
        assertEquals(1, Meta.read(dir)!!["runs"]!!.jsonArray.size)
    }

    @Test fun nodeChangeDiscardsItsJobAndUploads() {
        val dir = recording()
        pollReplies += MockResponse(200, body = """{"id":"j1","status":"running"}""")
        assertTrue(step(dir).first is StepResult.Again)
        MockWebServer().use { other ->
            val requests = CopyOnWriteArrayList<String>()
            other.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    requests += "${request.method} ${request.url.encodedPath}"
                    return server.dispatcher.dispatch(request)
                }
            }
            other.start()
            val url = other.url("/").toString().trimEnd('/') + "#token=new"
            assertEquals(StepResult.Done, step(dir, RunnerClient(parseConnection(url), http), settingsFor(url)).first)
            assertEquals(2, requests.count { it == "POST /api/uploads" })
            assertFalse(requests.any { it == "GET /api/jobs/j1" })
            assertNotEquals(submitted[0]["key"], submitted[1]["key"])
        }
    }

    @Test fun emptyTrackAndMissingSummaryTranscriptDoNotCreateJobs() {
        val dir = recording(system = false)
        File(dir, "mic.wav").writeBytes(WavFixture.header(0))
        assertEquals(StepResult.AudioOnly, step(dir).first)
        assertEquals(Problems.NO_SOUND, ProcessStateFile.read(dir)!!.problem)
        again(dir, summaryOnly = true)
        assertEquals(StepResult.Failed(Problems.NO_TRANSCRIPT), step(dir).first)
        assertTrue(submitted.isEmpty())
        assertEquals(0, encoded)
    }

    @Test fun tokenFailureDoesNotScheduleAnotherStep() {
        val dir = recording()
        submitReplies += MockResponse(401, body = "{}")
        assertEquals(StepResult.Failed(Problems.TOKEN), step(dir).first)
        val count = calls.size
        step(dir)
        assertEquals(count, calls.size)
    }

    @Test fun nodeAnswersAreHandledAsInTheSharedContract() {
        val answers = Json.parseToJsonElement(resource("recording-api-v1.json")).jsonObject.getValue("answers").jsonObject
        doneResult = answers.getValue("done").toString()
        fun reply(value: JsonElement) = (value as? JsonObject)?.let {
            MockResponse(it.getValue("status").jsonPrimitive.int, body = it.getValue("body").toString())
        }
        for ((i, case) in answers.getValue("cases").jsonArray.map { it.jsonObject }.withIndex()) {
            val label = case.getValue("case").jsonPrimitive.content
            submitted.clear(); uploaded.clear()
            var folder = recording(name = "2026-10-${10 + i}_16-01")
            val title = case["title"]?.jsonPrimitive?.content
            title?.let { nameProcessing(folder, it) }
            fun run(answers: JsonObject) {
                submitReplies.clear(); pollReplies.clear()
                answers["submit"]?.jsonArray?.forEach { submitReplies += reply(it) }
                answers["poll"]?.jsonArray?.forEach { pollReplies += reply(it) }
                var outcome: StepResult = StepResult.Again(0)
                repeat(10) {
                    if (outcome is StepResult.Again) step(folder).let { (result, next) -> outcome = result; folder = next }
                }
                val expected = answers.getValue("outcome").jsonPrimitive.content
                assertEquals(label, expected, when (outcome) { StepResult.Done -> "done"; is StepResult.Failed -> "failed"; else -> "$outcome" })
            }
            run(case)
            assertEquals(label, case.getValue("uploads").jsonArray.map { it.jsonPrimitive.content }, uploaded.map { it.substringBefore('.') })
            title?.let { assertEquals(label, it, ProcessStateFile.read(folder)!!.userTitle) }
            case["retry"]?.jsonObject?.let { retry(folder); run(it) }
            val keys = submitted.map { it.getValue("key").jsonPrimitive.content }
            val first = keys.distinct()
            assertEquals(label, case.getValue("keys").jsonArray.map { it.jsonPrimitive.int }, keys.map(first::indexOf))
        }
    }

    @Test fun refusedResultFailsWithoutPollingTheNodeAgainAsInTheSharedContract() {
        val cases = Json.parseToJsonElement(resource("recording-api-v1.json")).jsonObject.getValue("apply").jsonArray
            .map { it.jsonObject }.filter { it["refused"]?.jsonPrimitive?.boolean == true }
        assertTrue(cases.isNotEmpty())
        for ((i, case) in cases.withIndex()) {
            val label = case.getValue("case").jsonPrimitive.content
            val dir = recording(name = "2026-10-${10 + i}_12-30")
            val files = case.getValue("files").jsonObject.mapValues { it.value.jsonPrimitive.content }
            files.forEach { (name, text) -> File(dir, name).writeText(text) }
            summaryOnly = case.getValue("op").jsonPrimitive.content == "recording_summary"
            ProcessStateFile.write(dir, ProcessState("record-id", summaryOnly = summaryOnly))
            doneResult = case.getValue("result").toString()
            var outcome: StepResult = StepResult.Again(0)
            repeat(5) { if (outcome is StepResult.Again) outcome = step(dir).first }
            assertTrue(label, outcome is StepResult.Failed)
            assertEquals(label, files, files.mapValues { File(dir, it.key).readText() })
            assertFalse(label, File(dir.parentFile, "outside").exists())
            val before = calls.size
            assertTrue(label, step(dir).first is StepResult.Failed)
            assertEquals(label, before, calls.size)
        }
    }

    @Test fun terminalJobErrorCreatesAFreshJobOnManualRetry() {
        val dir = recording()
        pollReplies += MockResponse(200, body = """{"id":"j1","status":"error","error":"Не удалось распознать"}""")
        assertEquals(StepResult.Failed("Не удалось распознать"), step(dir).first)
        retry(dir)
        assertEquals(StepResult.Done, step(dir).first)
        assertNotEquals(submitted[0]["key"], submitted[1]["key"])
        assertEquals(2, uploaded.size)
    }

    private fun failedRun() = MockResponse(200, body =
        """{"id":"j1","status":"done","result":{"files":{},"run":{"status":"failed","error":"Нет места на узле"}}}""")

    @Test fun rejectedSubmissionIsManualRetryWithoutAnExtraPost() {
        val dir = recording()
        submitReplies += MockResponse(503, body = "{}")
        assertEquals(StepResult.Failed(nodeDown), step(dir).first)
        assertEquals(1, submitted.size)
        val count = calls.size
        step(dir)
        assertEquals(count, calls.size)
        retry(dir)
        assertEquals(StepResult.Done, step(dir).first)
        assertEquals(submitted.first()["key"], submitted.last()["key"])
    }

    @Test fun renameWhileNodeRunsKeepsTheUserName() {
        val dir = recording()
        pollReplies += MockResponse(200, body = """{"id":"j1","status":"running"}""")
        assertTrue(step(dir).first is StepResult.Again)
        val renamed = File(dir.parentFile, "Моя встреча")
        Files.move(dir.toPath(), renamed.toPath())
        assertEquals(StepResult.Done to renamed, step(renamed))
        assertEquals(dir.name, ProcessStateFile.read(renamed)!!.sentName)
        assertEquals(1, submitted.size)
    }

    @Test fun ownTitleBeforeTheJobIsSentGoesToTheNode() {
        val dir = recording()
        nameProcessing(dir, " Планёрка ")
        val (result, folder) = step(dir)
        assertEquals(StepResult.Done, result)
        assertEquals(JsonPrimitive("Планёрка"), submitted.single()["params"]!!.jsonObject["user_title"])
        assertEquals("2026-10-22_Планёрка", folder.name)
        assertFalse(File(dir.parentFile, "2026-10-22_Дейли").exists())
        assertNull(ProcessStateFile.read(folder)!!.userTitle)
    }

    @Test fun ownTitleAfterTheJobIsSentNamesTheFolderInsteadOfTheModel() {
        val dir = recording()
        pollReplies += MockResponse(200, body = """{"id":"j1","status":"running"}""")
        assertTrue(step(dir).first is StepResult.Again)
        assertEquals(JsonNull, submitted.single()["params"]!!.jsonObject["user_title"])
        nameProcessing(dir, "Планёрка")

        val (result, folder) = step(dir)

        assertEquals(StepResult.Done, result)
        assertEquals("2026-10-22_Планёрка", folder.name)
        assertFalse(dir.exists())
        assertFalse(File(dir.parentFile, "2026-10-22_Дейли").exists())
        assertEquals("[0:12] mic: x\n", File(folder, "transcript.txt").readText())
        assertEquals("j1", Meta.lastRun(folder)!!["job"]!!.jsonPrimitive.content)
        val state = ProcessStateFile.read(folder)!!
        assertEquals(ProcessStage.DONE, state.stage)
        assertNull(state.userTitle)
        assertEquals("DELETE /api/jobs/j1", calls.last())
    }

    @Test fun ownTitleSetWhileTheStepSendsTheJobIsNotLost() {
        val dir = recording()
        whileSubmitting = { nameProcessing(dir, "Планёрка") }
        val (result, folder) = step(dir)
        assertEquals(StepResult.Done, result)
        assertEquals("2026-10-22_Планёрка", folder.name)
    }

    @Test fun clearedOwnTitleLeavesTheNameToTheNode() {
        val dir = recording()
        // the title from the processing window and "Set title…" are one field: an empty one clears both
        again(dir, options = ReprocessOptions(title = "Из окна"))
        nameProcessing(dir, "Планёрка")
        nameProcessing(dir, "  ")
        val (_, folder) = step(dir)
        assertEquals(JsonNull, submitted.single()["params"]!!.jsonObject["user_title"])
        assertEquals("2026-10-22_Дейли", folder.name)
    }

    @Test fun ownTitleClearedAfterTheJobWentOutDoesNotNameTheFolder() {
        val dir = recording()
        nameProcessing(dir, "Планёрка")
        pollReplies += MockResponse(200, body = """{"id":"j1","status":"running"}""")
        assertTrue(step(dir).first is StepResult.Again)
        assertEquals(JsonPrimitive("Планёрка"), submitted.single()["params"]!!.jsonObject["user_title"])
        nameProcessing(dir, " ")

        // the server named the folder with the sent title ("Дейли" in the answer): a removed one does not name it
        val (result, folder) = step(dir)

        assertEquals(StepResult.Done, result)
        assertEquals(dir, folder)
        assertEquals("[0:12] mic: x\n", File(folder, "transcript.txt").readText())
    }

    @Test fun failedProcessingKeepsTheOwnTitleForTheRetry() {
        val dir = recording()
        nameProcessing(dir, "Планёрка")
        submitReplies += MockResponse(503, body = "{}")
        assertEquals(StepResult.Failed(nodeDown), step(dir).first)
        assertEquals("Планёрка", ProcessStateFile.read(dir)!!.userTitle)
        retry(dir)
        pollReplies += failedRun()
        assertEquals(StepResult.Failed("Нет места на узле"), step(dir).first)
        assertTrue(dir.isDirectory)
        assertEquals("Планёрка", ProcessStateFile.read(dir)!!.userTitle)
        retry(dir)

        val (result, folder) = step(dir)

        assertEquals(StepResult.Done, result)
        assertEquals(List(3) { JsonPrimitive("Планёрка") }, submitted.map { it["params"]!!.jsonObject["user_title"] })
        assertEquals("2026-10-22_Планёрка", folder.name)
    }

    @Test fun alreadyAppliedHistoryFinishesAfterProcessLossWithoutPollingOrWritingTexts() {
        val dir = recording()
        val (_, applied) = step(dir)
        val saved = ProcessStateFile.read(applied)!!
        ProcessStateFile.write(applied, saved.copy(stage = ProcessStage.ASR))
        File(applied, "transcript.txt").writeText("новая ручная правка")
        val before = calls.size
        assertEquals(StepResult.Done, step(applied).first)
        assertEquals(ProcessStage.DONE, ProcessStateFile.read(applied)!!.stage)
        assertEquals(listOf("DELETE /api/jobs/j1"), calls.drop(before))
        assertEquals("новая ручная правка", File(applied, "transcript.txt").readText())
    }

    @Test fun nodeCancellationErrorStopsWithoutFailureOrAutomaticRetry() {
        val dir = recording()
        pollReplies += MockResponse(200, body =
            """{"id":"j1","status":"error","error_kind":"cancelled","error":"Задача отменена"}""")
        assertEquals(StepResult.AudioOnly, step(dir).first)
        val state = ProcessStateFile.read(dir)!!
        assertEquals(ProcessStage.STOPPED, state.stage)
        assertNull(state.job)
        assertNull(state.key)
        assertNull(state.problem)
        assertTrue(Meta.read(dir)!!["runs"]!!.jsonArray.isEmpty())
        val before = calls.size
        assertEquals(StepResult.AudioOnly, step(dir).first)
        assertEquals(before, calls.size)
        retry(dir)
        assertEquals(StepResult.Done, step(dir).first)
        assertNotEquals(submitted.first()["key"], submitted.last()["key"])
    }

}
